from __future__ import annotations

import base64
import json
import os
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

from .ads import AdBeaconCounter, observe_player, scheduled_ads
from .innertube import InnerTubeRecorder, enrich_from_innertube
from .paths import PROJECT_BROWSER_PATH, SCREENSHOT_DIR, project_relative
from .safety import error_summary
from .selection import DEFAULT_DISPLAY_LIMIT, assign_display_ranks, filter_excluded_videos, select_next_video
from .time_utils import session_id_from_time, timestamp_pair
from .youtube import YouTubeClient


SCHEMA_VERSION = 3
MOBILE_BASE_URL = "https://m.youtube.com/"
MOBILE_SEARCH_URL = "https://m.youtube.com/results?search_query={query}&gl={region}&hl={language}"
FEED_CARDS_JS = (Path(__file__).parent / "js" / "feed_cards.js").read_text(encoding="utf-8")
SHORTS_ITEM_JS = (Path(__file__).parent / "js" / "shorts_item.js").read_text(encoding="utf-8")

DEVICE_OPTIONS = ("Pixel 7", "iPhone 13")
# YouTube's content location ("gl"), set like the Location choice in YouTube's settings; the IP,
# language, locale and time zone stay those of the machine running the browser.
LOCATION_OPTIONS = ("IE", "GB", "US")
CONSENT_OPTIONS = ("accept_all", "reject_all")
NAVIGATION_OPTIONS = ("tap", "goto")
EXCLUDE_POLICIES = ("displayed", "watched")
LABEL_CONFIDENCE = {"normal_video": 0.85, "short": 0.9, "ad": 0.9}
# Events passed to run_browser_observation(on_event=...). Every event has "type", "session_id"
# and "at"; "step" adds round/step/message (and screenshot for the landing shot), "round" adds
# round/record, "navigation" adds round/navigation, "finished" adds status/end_reason/error.
RUN_EVENT_TYPES = ("started", "step", "round", "navigation", "short", "finished")
ORGANIC_LABELS = {"normal_video", "short", "live", "upcoming", "movie"}
# QUIC (HTTP/3 over UDP) is switched off: VPNs, proxies and firewalls often break it part-way
# (net::ERR_QUIC_PROTOCOL_ERROR), while HTTP/2 over TCP, Chromium's own fallback, gets through.
BROWSER_ARGS = ("--mute-audio", "--disable-quic")
NET_ERROR_RE = re.compile(r"net::(ERR_[A-Z0-9_]+)")
FIRST_PAGE_ATTEMPTS = 2
FIRST_PAGE_RETRY_WAIT_MS = 2_000


class BrowserObservationError(RuntimeError):
    pass


class NetworkError(BrowserObservationError):
    """The browser could not reach YouTube at all (Chromium reported a net::ERR_... code)."""


class ConsentError(BrowserObservationError):
    """The cookie dialog could not be answered as requested; ``event`` describes the attempt."""

    def __init__(self, message: str, event: dict[str, Any]) -> None:
        super().__init__(message)
        self.event = event


class LocationMismatch(BrowserObservationError):
    """YouTube applied another content location than the requested one."""

    end_reason = "location_mismatch"


@dataclass(frozen=True)
class BrowserObservationSettings:
    strategy: str = "top_1"
    max_rounds: int = 5
    max_candidates: int = 20
    headless: bool = True
    slow_mo_ms: int = 250
    max_scroll_passes: int = 8
    device: str = "Pixel 7"
    consent_choice: str = "accept_all"
    navigation: str = "tap"
    exclude_policy: str = "displayed"
    # Time spent on each watch page (or Short) before reading it; the player is watched for
    # ads during it.
    watch_dwell_ms: int = 6_000
    check_shorts_urls: bool = True
    capture_innertube: bool = True
    # The content location (gl). The search URL carries it too, but YouTube drops URL
    # parameters when the cookie dialog reloads the page, so only the PREF cookie keeps it.
    region: str = "IE"
    location_cookie: bool = True
    language: str = "en"
    locale: str = "en-IE"
    timezone_id: str = "Europe/Dublin"
    youtube_api_key: str | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values.pop("youtube_api_key")
        values["youtube_api_enrichment"] = bool(self.youtube_api_key)
        return values


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def run_browser_observation(
    *,
    start_query: str,
    settings: BrowserObservationSettings,
    generated_queries: list[dict[str, Any]] | None = None,
    selected_query: str | None = None,
    start_url: str | None = None,
    parent: dict[str, Any] | None = None,
    screenshot_dir: Path = SCREENSHOT_DIR,
    system_prompt: str | None = None,
    user_prompt: str = "",
    openai_model: str | None = None,
    initial_seen_ids: Iterable[str] = (),
    checkpoint: Callable[[dict[str, Any]], Any] | None = None,
    on_event: Callable[[dict[str, Any]], Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_frame: Callable[[bytes], Any] | None = None,
    batch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one observation chain.

    Rounds are passed to ``checkpoint`` as they finish, and an error mid-run ends the
    chain with ``status="error"`` instead of discarding the rounds already observed.
    Only a browser that cannot start raises ``BrowserObservationError``.

    Live hooks for a UI (all optional; exceptions inside them are ignored):
    ``on_event`` receives progress events (see RUN_EVENT_TYPES), ``should_stop`` is polled
    before every round and navigation (a True answer ends the chain with
    ``end_reason="stopped"``), and ``on_frame`` receives JPEG frames of the phone screen.
    ``batch`` tags a run started by recpath.batch (batch id, condition, repeat).
    """
    start_video_id = video_id_from_url(start_url) if start_url else None
    seen_video_ids: set[str] = {video_id for video_id in initial_seen_ids if video_id}
    if start_video_id:
        seen_video_ids.add(start_video_id)

    session = new_session(
        "browser_observation",
        settings,
        start_query=start_query,
        start_url=start_url,
        generated_queries=generated_queries or [],
        selected_query=selected_query or start_query,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        openai_model=openai_model,
        parent=parent,
        initial_excluded_video_ids=sorted(seen_video_ids),
        batch=batch,
    )
    youtube_client = (
        YouTubeClient(settings.youtube_api_key, region=settings.region, language=settings.language)
        if settings.youtube_api_key
        else None
    )
    run = BrowserRun(session, settings, screenshot_dir, checkpoint, on_event, should_stop, on_frame)

    def chain(tools: RunTools) -> None:
        page = tools.page
        run.emit("step", round=None, step="opening", message="Opening YouTube")
        open_first_page(page, start_url or search_url(start_query, settings.region, settings.language))
        run.emit("step", round=None, step="consent", message="Checking the cookie dialog")
        apply_consent(session, page, settings.consent_choice, wait_for_dialog_ms=4_000)
        if start_url:
            wait_for_watch_ready(page, start_video_id, [], tools.timeout_error)
        session["client"] = describe_client(page, settings, tools.browser, tools.device)
        shorts_checker = ShortsUrlChecker(page.context) if settings.check_shorts_urls else None

        for round_number in range(1, settings.max_rounds + 1):
            run.current_round = round_number
            if run.stop_requested():
                session["end_reason"] = "stopped"
                break
            source = (
                "search_results"
                if round_number == 1 and not start_url
                else "watch_recommendations"
            )
            page_kind = "search" if source == "search_results" else "watch"
            fetched_at, fetched_at_local = timestamp_pair()
            apply_consent(session, page, settings.consent_choice, round_number=round_number)

            location = verify_location(page, settings)

            stem = f"{session['session_id']}_round_{round_number:02d}"
            landing_screenshot = take_screenshot(page, run.screenshot_dir / f"{stem}.jpg")
            observation = None
            if page_kind == "watch":
                # Stay on the video like a viewer would, watching the player for ads meanwhile.
                run.emit("step", round=round_number, step="observing", message="Watching the video",
                         screenshot=landing_screenshot)
                observation = observe_player(
                    page,
                    settings.watch_dwell_ms,
                    on_first_ad=lambda: take_screenshot(page, run.screenshot_dir / f"{stem}_ad.jpg"),
                    should_stop=run.stop_requested,
                )
            run.emit("step", round=round_number, step="scrolling", message="Reading the feed",
                     screenshot=landing_screenshot)
            feed = collect_feed(
                page,
                page_kind=page_kind,
                max_candidates=settings.max_candidates,
                max_scroll_passes=settings.max_scroll_passes,
                timeout_error=tools.timeout_error,
            )
            if not feed["scope_found"]:
                # The feed section can render late; look once more before giving up.
                page.wait_for_timeout(2_000)
                feed = collect_feed(
                    page,
                    page_kind=page_kind,
                    max_candidates=settings.max_candidates,
                    max_scroll_passes=settings.max_scroll_passes,
                    timeout_error=tools.timeout_error,
                )
            if tools.recorder:
                tools.recorder.ingest_page_globals(page)
            raw_results = normalize_feed_items(
                feed["items"],
                source=source,
                page_kind=page_kind,
                current_video_id=feed["current_video_id"],
                viewport_height=feed["viewport_height"],
                max_candidates=settings.max_candidates,
            )
            raw_results = classify_shorts(enrich_from_innertube(raw_results, tools.recorder), shorts_checker)
            enriched_raw_results, enrichment_error = enrich_browser_results(
                raw_results,
                youtube_client,
            )
            filtered_results, filtered_out_video_ids = filter_excluded_videos(
                enriched_raw_results,
                seen_video_ids,
            )
            results = assign_display_ranks(filtered_results)
            normal_candidates = [
                item for item in results if item.get("label") == "normal_video"
            ]
            selected, selection = select_next_video(normal_candidates, settings.strategy)
            selection["exclude_policy"] = settings.exclude_policy

            selected_screenshot = None
            if selected:
                selected_screenshot = capture_selected_card(
                    page,
                    page_kind,
                    selected["video_id"],
                    run.screenshot_dir / f"{stem}_selected.jpg",
                )

            shorts = [item for item in results if item.get("label") == "short"]
            label_counts = dict(Counter(item.get("label") for item in raw_results))
            ads = round_ads(observation, tools, feed["current_video_id"], label_counts.get("ad", 0))
            round_record = {
                "round": round_number,
                "source": source,
                "page_url": page.url,
                "current_video_id": feed["current_video_id"],
                "fetched_at": fetched_at,
                "fetched_at_local": fetched_at_local,
                "location": location,
                "feed_scope_found": feed["scope_found"],
                "stale_card_count": sum(1 for item in feed["items"] if item.get("stale")),
                "scroll": feed["scroll"],
                "raw_result_count": len(raw_results),
                "filtered_result_count": len(results),
                "filtered_duplicate_count": len(filtered_out_video_ids),
                "label_counts": label_counts,
                "metadata_enriched": youtube_client is not None and enrichment_error is None,
                "metadata_enrichment_error": enrichment_error,
                "excluded_video_ids": sorted(seen_video_ids),
                "filtered_out_video_ids": filtered_out_video_ids,
                "screenshot_path": landing_screenshot,
                "screenshots": {
                    "landing": landing_screenshot,
                    "selected": selected_screenshot,
                    "ad": ((observation or {}).get("player") or {}).get("screenshot"),
                },
                "ads": ads,
                "watch_cut_short": bool((observation or {}).get("stopped_early")),
                "raw_results": enriched_raw_results,
                "results": results,
                "top_normal_videos": normal_candidates[:DEFAULT_DISPLAY_LIMIT],
                "shorts_results": shorts,
                "shorts_result_count": len(shorts),
                "selected_result": selected,
                "selection": selection,
                "navigation": None,
            }
            session["rounds"].append(round_record)
            run.emit("round", round=round_number, record=round_record)

            if settings.exclude_policy == "displayed":
                for result in enriched_raw_results:
                    if result.get("video_id"):
                        seen_video_ids.add(result["video_id"])

            if not selected:
                session["end_reason"] = "no_candidates" if feed["scope_found"] else "feed_not_found"
                break

            seen_video_ids.add(selected["video_id"])
            run.save()
            # Checked before the max-rounds test, so a stop in the last round is recorded too.
            if run.stop_requested():
                session["end_reason"] = "stopped"
                break
            if round_number == settings.max_rounds:
                break

            # Attached before navigating, so a failed navigation still leaves its record.
            round_record["navigation"] = new_navigation_record(selected)
            run.emit("step", round=round_number, step="navigating",
                     message=f"Opening {selected.get('title') or selected['video_id']}")
            navigate_to_selected(
                page,
                selected,
                page_kind,
                settings,
                tools.timeout_error,
                round_record["navigation"],
            )
            run.emit("navigation", round=round_number, navigation=round_record["navigation"])
            run.save()

    return run.execute(chain)


def run_shorts_feed(
    *,
    start_query: str,
    settings: BrowserObservationSettings,
    items: int = 10,
    screenshot_dir: Path = SCREENSHOT_DIR,
    checkpoint: Callable[[dict[str, Any]], Any] | None = None,
    on_event: Callable[[dict[str, Any]], Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_frame: Callable[[bytes], Any] | None = None,
) -> dict[str, Any]:
    """Open the first Short from a search and swipe through the Shorts feed, recording each item.

    Every item records its video id, title, channel, whether it is an ad (and which advertiser),
    a screenshot and how long it was watched. Ads are never tapped.
    """
    session = new_session(
        "shorts_feed",
        settings,
        start_query=start_query,
        start_url=None,
        selected_query=start_query,
        shorts_feed=[],
    )
    # Chain-only settings do not apply to a Shorts feed (and no Data API enrichment happens).
    for name in ("strategy", "max_rounds", "max_candidates", "max_scroll_passes", "navigation", "exclude_policy"):
        session["settings"].pop(name, None)
    session["settings"]["youtube_api_enrichment"] = False
    session["settings"]["shorts_items"] = items
    session["strategy"] = None
    run = BrowserRun(session, settings, screenshot_dir, checkpoint, on_event, should_stop, on_frame)

    def feed(tools: RunTools) -> None:
        page = tools.page
        run.emit("step", round=None, step="opening", message="Opening YouTube")
        open_first_page(page, search_url(start_query, settings.region, settings.language))
        run.emit("step", round=None, step="consent", message="Checking the cookie dialog")
        apply_consent(session, page, settings.consent_choice, wait_for_dialog_ms=4_000)
        session["client"] = describe_client(page, settings, tools.browser, tools.device)
        location = verify_location(page, settings)
        session["shorts_start"] = open_first_short(page, settings, tools.timeout_error)
        session["shorts_start"]["location"] = location

        for position in range(1, max(items, 1) + 1):
            run.current_round = position
            if run.stop_requested():
                session["end_reason"] = "stopped"
                break
            run.emit("step", round=position, step="shorts", message=f"Watching Short {position}")
            started = time.monotonic()
            page.wait_for_timeout(settings.watch_dwell_ms)
            state = page.evaluate(SHORTS_ITEM_JS)
            stem = f"{session['session_id']}_short_{position:02d}"
            item = {
                "position": position,
                "video_id": state.get("video_id"),
                "url": state.get("url"),
                "title": clean_optional_text(state.get("title")),
                "channel_name": clean_optional_text(state.get("channel_name")),
                "is_ad": bool(state.get("is_ad")),
                "ad": state.get("ad"),
                "screenshot": take_screenshot(page, run.screenshot_dir / f"{stem}.jpg"),
                "dwell_ms": round((time.monotonic() - started) * 1000),
                "at": timestamp_pair()[0],
                "innertube": None,
            }
            if tools.recorder and item["video_id"]:
                known = tools.recorder.items.get(item["video_id"])
                if known:
                    item["innertube"] = {k: known.get(k) for k in ("channel_id", "view_count_text", "renderer", "endpoint")}
            session["shorts_feed"].append(item)
            run.emit("short", round=position, item=item)
            run.save()
            if position == items:
                break
            if not advance_shorts_feed(page, tools.timeout_error):
                session["end_reason"] = "feed_stuck"
                break
        session["ad_beacons"] = tools.beacons.take()

    return run.execute(feed)


def new_session(mode: str, settings: BrowserObservationSettings, **fields: Any) -> dict[str, Any]:
    created_at, created_at_local = timestamp_pair()
    session: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id_from_time(),
        "created_at": created_at,
        "created_at_local": created_at_local,
        "finished_at": None,
        "mode": mode,
        "task_type": "real_time",
        "status": "running",
        "end_reason": None,
        "error": None,
        "start_query": None,
        "start_url": None,
        "region": settings.region,
        "language": settings.language,
        "strategy": settings.strategy,
        "settings": settings.public_dict(),
        "client": None,
        "consent": {"choice": settings.consent_choice, "checked": False, "events": []},
        "generated_queries": [],
        "selected_query": None,
        "system_prompt": None,
        "user_prompt": "",
        "openai_model": None,
        "parent": None,
        "initial_excluded_video_ids": [],
        "batch": None,
        "rounds": [],
    }
    session.update(fields)
    return session


@dataclass
class RunTools:
    page: Any
    browser: Any
    device: dict[str, Any]
    recorder: InnerTubeRecorder | None
    beacons: AdBeaconCounter
    timeout_error: type[Exception]


class BrowserRun:
    """Shared plumbing for one browser run: hooks, checkpoints, browser lifecycle, errors."""

    def __init__(
        self,
        session: dict[str, Any],
        settings: BrowserObservationSettings,
        screenshot_dir: Path,
        checkpoint: Callable[[dict[str, Any]], Any] | None,
        on_event: Callable[[dict[str, Any]], Any] | None,
        should_stop: Callable[[], bool] | None,
        on_frame: Callable[[bytes], Any] | None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.screenshot_dir = screenshot_dir
        self.checkpoint = checkpoint
        self.on_event = on_event
        self.should_stop = should_stop
        self.on_frame = on_frame
        self.current_round = 0

    def emit(self, event_type: str, **payload: Any) -> None:
        if not self.on_event:
            return
        try:
            self.on_event(
                {"type": event_type, "session_id": self.session["session_id"], "at": timestamp_pair()[0], **payload}
            )
        except Exception:
            pass

    def stop_requested(self) -> bool:
        try:
            return bool(self.should_stop and self.should_stop())
        except Exception:
            return False

    def save(self) -> None:
        # A failed save must not end the run: the session stays in memory and is returned.
        if not self.checkpoint:
            return
        try:
            self.checkpoint(self.session)
            self.session["last_save_ok"] = True
        except Exception as exc:
            self.session["last_save_ok"] = False
            self.session.setdefault("save_errors", []).append(
                {"rounds_saved": len(self.session["rounds"]), "message": error_summary(exc)}
            )

    def execute(self, body: Callable[[RunTools], None]) -> dict[str, Any]:
        ensure_project_browser_path()
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserObservationError(
                "Playwright is not installed. Install dependencies with "
                "'python -m pip install -r requirements.txt', then run "
                "'python -m playwright install chromium'."
            ) from exc

        session = self.session
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.emit("started", settings=session["settings"], start_query=session.get("start_query"),
                  start_url=session.get("start_url"), mode=session["mode"])
        with sync_playwright() as p:
            browser, context, device = launch_browser(p, self.settings)
            if self.settings.location_cookie:
                set_location_cookie(context, self.settings.region)
            page = context.new_page()
            page.set_default_timeout(12_000)
            if self.on_frame:
                start_screencast(context, page, self.on_frame)
            recorder = InnerTubeRecorder() if self.settings.capture_innertube else None
            if recorder:
                recorder.attach(context)
            beacons = AdBeaconCounter()
            beacons.attach(context)
            try:
                body(RunTools(page, browser, device, recorder, beacons, PlaywrightTimeoutError))
                session["end_reason"] = session["end_reason"] or "max_rounds"
                session["status"] = "completed"
            except Exception as exc:
                session["status"] = "error"
                session["end_reason"] = getattr(exc, "end_reason", "error")
                session["error"] = {
                    "round": self.current_round or None,
                    "message": error_summary(exc),
                    "page_url": safe_page_url(page),
                }
            except BaseException:
                # An interrupt raised in Python code (e.g. KeyboardInterrupt between browser calls):
                # keep what was observed, then stop. A Ctrl-C in the terminal usually kills the
                # browser first, which ends the run as an "error" (TargetClosedError) instead.
                session["status"] = "interrupted"
                session["end_reason"] = "interrupted"
                raise
            finally:
                session["finished_at"] = timestamp_pair()[0]
                if recorder:
                    session["innertube_capture"] = {
                        "responses": recorder.responses_seen,
                        "parse_errors": recorder.errors,
                        "items_indexed": len(recorder.items),
                        "player_responses": sorted(recorder.player_responses),
                    }
                for closable in (context, browser):
                    try:
                        closable.close()
                    except Exception:
                        pass
                self.save()

        self.emit("finished", status=session["status"], end_reason=session["end_reason"], error=session["error"])
        return session


def round_ads(
    observation: dict[str, Any] | None,
    tools: RunTools,
    current_video_id: str | None,
    feed_ads: int,
) -> dict[str, Any]:
    """Everything known about ads on this round's page (see recpath/ads.py)."""
    player_response = tools.recorder.player_response(current_video_id, tools.page) if tools.recorder else None
    scheduled, paid = scheduled_ads(player_response)
    observation = observation or {}
    return {
        "observed_ms": observation.get("observed_ms", 0),
        "samples": observation.get("samples", 0),
        "player": observation.get("player"),
        "companion": observation.get("companion"),
        "scheduled": scheduled,
        "paid_promotion": (
            bool(observation.get("paid_promotion")) or bool(paid) if observation or paid is not None else None
        ),
        "feed_ads": feed_ads,
        "beacons": tools.beacons.take(),
    }


class ShortsUrlChecker:
    """Asks YouTube whether a video is a Short: /shorts/<id> answers 200 for a Short and
    redirects to /watch for anything else. Used only for short videos shown as normal cards."""

    def __init__(self, context: Any, max_checks_per_round: int = 10) -> None:
        self.request = context.request
        self.cache: dict[str, bool | None] = {}
        self.max_checks = max_checks_per_round
        self.checks_this_round = 0

    def new_round(self) -> None:
        self.checks_this_round = 0

    def is_short(self, video_id: str) -> bool | None:
        if video_id in self.cache:
            return self.cache[video_id]
        if self.checks_this_round >= self.max_checks:
            return None
        self.checks_this_round += 1
        try:
            response = self.request.fetch(
                f"https://m.youtube.com/shorts/{video_id}", method="HEAD", max_redirects=0, timeout=8_000
            )
            status = response.status
            result = True if status == 200 else False if 300 <= status < 400 else None
        except Exception:
            result = None
        self.cache[video_id] = result
        return result


def classify_shorts(
    results: list[dict[str, Any]],
    checker: ShortsUrlChecker | None = None,
) -> list[dict[str, Any]]:
    """Label Shorts from every available signal and record which signals were used.

    Signals: a /shorts/ link or Shorts card (DOM), a reel endpoint in YouTube's page data, and
    for normal-looking videos of at most 3 minutes, YouTube's own /shorts/<id> answer. Duration
    alone never decides: since Oct 2024 a Short can be up to 3 minutes, and a short video is not
    necessarily a Short.
    """
    if checker:
        checker.new_round()
    classified = []
    for result in results:
        item = result.copy()
        evidence: list[str] = []
        url = item.get("observed_url") or item.get("url") or ""
        card_tag = item.get("card_tag") or ""
        if "/shorts/" in url:
            evidence.append("dom:shorts_link")
        if "shorts" in card_tag or "reel" in card_tag:
            evidence.append("dom:shorts_card")
        if item.get("time_style") == "SHORTS":
            evidence.append("dom:shorts_badge")
        if (item.get("innertube") or {}).get("endpoint") == "reel":
            evidence.append("innertube:reel_endpoint")
        seconds = item.get("duration_seconds")
        item["duration_bucket"] = (
            None if seconds is None else "<=60s" if seconds <= 60 else "61-180s" if seconds <= 180 else ">180s"
        )
        if item.get("label") == "normal_video" and item.get("video_id"):
            if evidence:
                item["label"] = "short"
            elif checker and seconds is not None and seconds <= 180:
                answer = checker.is_short(item["video_id"])
                if answer is not None:
                    evidence.append(f"shorts_url:{'200' if answer else 'redirect'}")
                if answer:
                    item["label"] = "short"
        item["shorts_evidence"] = evidence
        item["presentation"] = presentation_of(item)
        classified.append(item)
    return classified


def presentation_of(item: dict[str, Any]) -> str:
    card_tag = item.get("card_tag") or ""
    if item.get("shelf"):
        return "shorts_shelf" if item.get("label") == "short" else "shelf"
    if "shorts" in card_tag or "reel" in card_tag or "/shorts/" in (item.get("observed_url") or ""):
        return "shorts_card"
    if "video" in card_tag or card_tag == "yt-lockup-view-model":
        return "video_card"
    return "other"


def open_first_short(page: Any, settings: BrowserObservationSettings, timeout_error: type[Exception]) -> dict[str, Any]:
    """Tap the first Short in the search feed (or open the Shorts tab if there is none)."""
    feed = collect_feed(page, page_kind="search", max_candidates=5, max_scroll_passes=2, timeout_error=timeout_error)
    first = next((item for item in feed["items"] if item.get("label") == "short" and item.get("video_id")), None)
    record: dict[str, Any] = {"video_id": first.get("video_id") if first else None, "method": None}
    if first:
        try:
            link = page.evaluate_handle(
                FEED_CARDS_JS, {"mode": "link", "pageKind": "search", "videoId": first["video_id"]}
            ).as_element()
            link.scroll_into_view_if_needed(timeout=3_000)
            link.tap(timeout=5_000)
            page.wait_for_url(re.compile(r"/shorts/"), timeout=10_000)
            record["method"] = "tap"
        except Exception as exc:
            record["fallback_reason"] = error_summary(exc, 200)
            page.goto(f"https://m.youtube.com/shorts/{first['video_id']}", wait_until="domcontentloaded")
            record["method"] = "goto"
    else:
        page.goto("https://m.youtube.com/shorts", wait_until="domcontentloaded")
        page.wait_for_url(re.compile(r"/shorts/"), timeout=10_000)
        record["method"] = "shorts_tab"
    record["url"] = page.url
    return record


def advance_shorts_feed(page: Any, timeout_error: type[Exception]) -> bool:
    """Move to the next Short (keyboard first, then a swipe). False if the feed did not move."""
    before = page.url
    for gesture in ("key", "wheel"):
        if gesture == "key":
            page.keyboard.press("ArrowDown")
        else:
            page.mouse.wheel(0, 800)
        try:
            page.wait_for_function("(before) => location.href !== before", arg=before, timeout=5_000)
            return True
        except timeout_error:
            continue
    return False


def start_screencast(context: Any, page: Any, on_frame: Callable[[bytes], Any]) -> Any:
    """Mirror the phone screen as JPEG frames (CDP screencast; Chromium only)."""
    cdp = context.new_cdp_session(page)

    def handle(params: dict[str, Any]) -> None:
        try:
            cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:
            pass
        try:
            on_frame(base64.b64decode(params["data"]))
        except Exception:
            pass

    cdp.on("Page.screencastFrame", handle)
    cdp.send(
        "Page.startScreencast",
        {"format": "jpeg", "quality": 60, "maxWidth": 430, "maxHeight": 932, "everyNthFrame": 2},
    )
    return cdp


def search_url(query: str, region: str = "IE", language: str = "en") -> str:
    return MOBILE_SEARCH_URL.format(query=quote_plus(query), region=region, language=language)


def open_first_page(page: Any, url: str) -> None:
    """Open a run's first page, trying again once if the network drops the connection.

    A net::ERR_... failure here means the browser never reached YouTube, which is a problem
    with this computer's connection (VPN, proxy, firewall), so it is reported as one.
    """
    for attempt in range(1, FIRST_PAGE_ATTEMPTS + 1):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return
        except Exception as exc:
            match = NET_ERROR_RE.search(str(exc))
            if not match:
                raise
            if attempt < FIRST_PAGE_ATTEMPTS:
                page.wait_for_timeout(FIRST_PAGE_RETRY_WAIT_MS)
                continue
            raise NetworkError(
                f"Could not reach YouTube ({match.group(1)}, tried {FIRST_PAGE_ATTEMPTS} times). This is a "
                "problem with this computer's network connection, not with the run settings: check the "
                "VPN, proxy or firewall, or try another network, then start the run again"
            ) from exc


def ensure_project_browser_path() -> None:
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        return
    if PROJECT_BROWSER_PATH.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PROJECT_BROWSER_PATH)


def launch_browser(p: Any, settings: BrowserObservationSettings) -> tuple[Any, Any, dict[str, Any]]:
    device = dict(p.devices.get(settings.device) or {})
    if not device:
        raise BrowserObservationError(f"Unknown Playwright device profile: {settings.device!r}")
    launch_options = {
        "headless": settings.headless,
        "slow_mo": settings.slow_mo_ms,
        "args": list(BROWSER_ARGS),
    }
    # channel="chromium" runs the full browser in new headless mode, so the client hints
    # do not advertise "HeadlessChrome". Fall back to the headless shell if it is missing.
    attempts = [{"channel": "chromium"}, {}] if settings.headless else [{}]
    browser = None
    last_error: Exception | None = None
    for extra in attempts:
        try:
            browser = p.chromium.launch(**launch_options, **extra)
            break
        except Exception as exc:
            last_error = exc
    if browser is None:
        raise BrowserObservationError(
            "Playwright could not launch Chromium. Run the browser install command from step 1 "
            "of the README again, from the project folder, then restart the app. "
            f"Original launch error: {error_summary(last_error, 900) if last_error else 'unknown'}"
        )
    context_options = {key: value for key, value in device.items() if key != "default_browser_type"}
    context = browser.new_context(
        **context_options,
        locale=settings.locale,
        timezone_id=settings.timezone_id,
    )
    return browser, context, device


def set_location_cookie(context: Any, region: str) -> None:
    """Choose YouTube's content location the way its Location setting stores it (PREF gl=..).

    YouTube adds its own PREF fields (such as tz) to the cookie and keeps gl when the cookie
    dialog is answered, so the location holds on every later page, tapped or opened by URL.
    """
    context.add_cookies(
        [{"name": "PREF", "value": f"gl={region}", "domain": ".youtube.com", "path": "/", "secure": True}]
    )


def observe_location(page: Any) -> dict[str, Any]:
    """The content location this page uses: gl/hl in YouTube's client config and gl in PREF."""
    try:
        observed = page.evaluate(
            """
            () => {
              const context = (window.ytcfg && ytcfg.get && ytcfg.get("INNERTUBE_CONTEXT")) || {};
              const client = context.client || {};
              return {gl: client.gl || null, hl: client.hl || null};
            }
            """
        )
    except Exception:
        observed = {"gl": None, "hl": None}
    pref = next(
        (cookie.get("value") or "" for cookie in page.context.cookies("https://m.youtube.com") if cookie.get("name") == "PREF"),
        "",
    )
    observed["pref_gl"] = (parse_qs(pref).get("gl") or [None])[0]
    return observed


def verify_location(page: Any, settings: BrowserObservationSettings) -> dict[str, Any]:
    """Record the content location of this page and end the run if YouTube applied another one.

    ``ok`` is None when the page shows no client config (nothing to compare); a different gl,
    or a PREF cookie that lost the requested gl, raises LocationMismatch.
    """
    observed = observe_location(page)
    location = {"requested": settings.region, **observed, "ok": None}
    if observed["gl"]:
        location["ok"] = observed["gl"] == settings.region and (
            not settings.location_cookie or observed["pref_gl"] == settings.region
        )
    if location["ok"] is False:
        raise LocationMismatch(
            f"YouTube applied content location {observed['gl']!r} (PREF gl {observed['pref_gl']!r}), "
            f"not the requested {settings.region!r}."
        )
    return location


def describe_client(
    page: Any,
    settings: BrowserObservationSettings,
    browser: Any,
    device: dict[str, Any],
) -> dict[str, Any]:
    """Record what YouTube actually saw, so runs on different setups can be compared honestly."""
    observed = page.evaluate(
        """
        () => {
          const context = (window.ytcfg && ytcfg.get && ytcfg.get("INNERTUBE_CONTEXT")) || {};
          const client = context.client || {};
          const uaData = navigator.userAgentData;
          return {
            user_agent: navigator.userAgent,
            ua_brands: uaData ? uaData.brands.map((b) => `${b.brand} ${b.version}`) : null,
            ua_platform: uaData ? uaData.platform : null,
            innertube_client: {
              client_name: client.clientName || null,
              client_version: client.clientVersion || null,
              gl: client.gl || null,
              hl: client.hl || null,
            },
          };
        }
        """
    )
    return {
        "device": settings.device,
        "device_browser_type": device.get("default_browser_type"),
        "engine": "chromium",
        "engine_matches_device": device.get("default_browser_type") == "chromium",
        "browser_version": browser.version,
        "browser_args": list(BROWSER_ARGS),
        "headless": settings.headless,
        "viewport": device.get("viewport"),
        "device_scale_factor": device.get("device_scale_factor"),
        "locale": settings.locale,
        "timezone_id": settings.timezone_id,
        **observed,
    }


def has_consent_cookie(context: Any) -> bool:
    return any(cookie.get("name") == "SOCS" for cookie in context.cookies())


def resolve_consent(
    page: Any,
    choice: str,
    *,
    wait_for_dialog_ms: int = 0,
) -> dict[str, Any] | None:
    """Answer YouTube's EU cookie dialog with the configured choice and report what happened.

    Returns None when no dialog was shown. The dialog's buttons are named by long aria-labels
    ("Reject the use of cookies and other data ..."), so match on the leading verb.
    """
    if has_consent_cookie(page.context):
        return None
    on_consent_page = "consent." in urlparse(page.url).netloc
    dialog = page.locator("ytm-consent-bump-v2-renderer")
    if not on_consent_page:
        if wait_for_dialog_ms:
            try:
                dialog.first.wait_for(state="attached", timeout=wait_for_dialog_ms)
            except Exception:
                pass
        if dialog.count() == 0:
            return None

    verb = "Reject" if choice == "reject_all" else "Accept"
    name = re.compile(rf"^\s*{verb}(\s+all|\s+the use of cookies)", re.IGNORECASE)
    scope = page if on_consent_page else dialog.first
    button = scope.get_by_role("button", name=name).first
    event: dict[str, Any] = {
        "surface": "consent_page" if on_consent_page else "in_page_dialog",
        "choice": choice,
        "page_url": page.url,
        "clicked_label": None,
        "ok": False,
        "at": timestamp_pair()[0],
    }
    try:
        # The dialog can attach before its buttons render.
        scope.get_by_role("button").first.wait_for(state="visible", timeout=10_000)
        event["clicked_label"] = button.get_attribute("aria-label", timeout=3_000) or button.inner_text(timeout=3_000)
        button.scroll_into_view_if_needed(timeout=3_000)
        button.click(timeout=3_000)
        if on_consent_page:
            page.wait_for_url(lambda url: "consent." not in urlparse(url).netloc, timeout=10_000)
        else:
            try:
                dialog.first.wait_for(state="hidden", timeout=5_000)
            except Exception:
                # A click that lands before the dialog is wired up does nothing: try once more.
                event["retried"] = True
                try:
                    button.click(timeout=3_000)
                except Exception:
                    pass
                dialog.first.wait_for(state="hidden", timeout=5_000)
        # Answering the in-page dialog can reload the page; let that finish before reading it.
        page.wait_for_timeout(500)
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception as exc:
        event["error"] = error_summary(exc, 200)

    still_on_consent_page = "consent." in urlparse(page.url).netloc
    event["ok"] = has_consent_cookie(page.context) and not still_on_consent_page
    if not event["ok"]:
        # Collecting data under a consent state other than the requested one would mislabel it.
        raise ConsentError(
            f"Could not answer the YouTube cookie dialog with {choice} "
            f"({event.get('error') or 'no consent cookie was set'}).",
            event,
        )
    return event


def apply_consent(
    session: dict[str, Any],
    page: Any,
    choice: str,
    *,
    round_number: int | None = None,
    wait_for_dialog_ms: int = 0,
) -> None:
    """Resolve the cookie dialog and record the attempt on the session, failed ones included."""
    try:
        event = resolve_consent(page, choice, wait_for_dialog_ms=wait_for_dialog_ms)
    except ConsentError as exc:
        record_consent(session, exc.event, round_number)
        raise
    finally:
        session["consent"]["checked"] = True
    record_consent(session, event, round_number)


def record_consent(
    session: dict[str, Any],
    event: dict[str, Any] | None,
    round_number: int | None = None,
) -> None:
    if event:
        session["consent"]["events"].append({**event, "round": round_number})
        session["consent"]["applied"] = event.get("ok", False)


def take_screenshot(page: Any, path: Path) -> str:
    page.screenshot(path=str(path), full_page=False, type="jpeg", quality=80)
    return project_relative(path)


def read_feed(page: Any, page_kind: str) -> dict[str, Any]:
    return page.evaluate(FEED_CARDS_JS, {"mode": "extract", "pageKind": page_kind})


def count_feed_cards_expression(page_kind: str) -> str:
    return f"(n) => ({FEED_CARDS_JS})({{mode: 'count', pageKind: {json.dumps(page_kind)}}}) > n"


def collect_feed(
    page: Any,
    *,
    page_kind: str,
    max_candidates: int,
    max_scroll_passes: int,
    timeout_error: type[Exception],
) -> dict[str, Any]:
    """Scroll to the end of the feed until enough normal videos are rendered or loading stalls.

    Each scroll to the bottom triggers YouTube's continuation request (about 12 more items on
    watch pages); a fixed short scroll never reaches it.
    """
    feed = read_feed(page, page_kind)
    passes = 0
    stalls = 0
    while count_normal_candidates(feed) < max_candidates and passes < max_scroll_passes:
        before = page.evaluate(FEED_CARDS_JS, {"mode": "count", "pageKind": page_kind})
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        passes += 1
        try:
            page.wait_for_function(count_feed_cards_expression(page_kind), arg=before, timeout=3_000)
            stalls = 0
        except timeout_error:
            stalls += 1
        feed = read_feed(page, page_kind)
        if stalls >= 2:
            break
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    normal_found = count_normal_candidates(feed)
    feed["scroll"] = {
        "passes": passes,
        "stalled": stalls >= 2,
        "normal_candidates_found": normal_found,
        "cap_reached": normal_found >= max_candidates,
        "document_height": feed.get("document_height"),
        "rendered_cards": len(feed["items"]),
        "section_count": feed.get("section_count"),
        "unmatched_links": feed.get("unmatched_links"),
    }
    return feed


def count_normal_candidates(feed: dict[str, Any]) -> int:
    """How many normal videos normalize_feed_items will keep from this feed (same rules)."""
    seen: set[str] = set()
    count = 0
    for item in feed["items"]:
        video_id = item.get("video_id")
        if (item.get("label") or "normal_video") not in ORGANIC_LABELS or not video_id:
            continue
        if video_id == feed.get("current_video_id") or video_id in seen:
            continue
        seen.add(video_id)
        if item.get("label") == "normal_video":
            count += 1
    return count


def normalize_feed_items(
    items: list[dict[str, Any]],
    *,
    source: str,
    page_kind: str,
    current_video_id: str | None,
    viewport_height: int | None,
    max_candidates: int,
) -> list[dict[str, Any]]:
    """Turn extracted cards into result records in feed order.

    Keeps every card type (Shorts, ads, playlists, live, movies) so the feed composition is
    complete, drops the playing video and duplicates, and stops after the card that holds the
    ``max_candidates``-th normal video.
    """
    results: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    normal_count = 0

    for item in items:
        label = item.get("label") or "normal_video"
        video_id = item.get("video_id")
        organic = label in ORGANIC_LABELS
        if label in {"normal_video", "short"} and not video_id:
            continue
        if organic and video_id and video_id == current_video_id:
            continue
        if video_id:
            # Organic cards share one id space (the first appearance wins); an ad or a Mix
            # seeded by a video must not hide that video's own organic card.
            key = ("organic", video_id) if organic else (label, video_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)

        observed_url = urljoin(MOBILE_BASE_URL, item["href"]) if item.get("href") else None
        raw_position = len(results) + 1
        doc_top = item.get("doc_top")
        results.append(
            {
                "position": raw_position,
                "raw_position": raw_position,
                "display_rank": None,
                "label": label,
                "title": clean_title(item.get("title") or item.get("title_aria") or ""),
                "url": ensure_mobile_url(observed_url) if observed_url else None,
                "observed_url": observed_url,
                "canonical_url": canonical_watch_url(video_id) if video_id else None,
                "video_id": video_id,
                "playlist_id": item.get("playlist_id"),
                "start_radio": item.get("start_radio", False),
                "channel_name": clean_optional_text(item.get("channel_name")),
                "channel_url": urljoin(MOBILE_BASE_URL, item["channel_url"]) if item.get("channel_url") else None,
                "channel_id": None,
                "published_at": None,
                "duration": None,
                "duration_text": clean_optional_text(item.get("duration_text")),
                "duration_seconds": parse_duration_text(item.get("duration_text")),
                "view_count": None,
                "like_count": None,
                "comment_count": None,
                "source": source,
                "metadata_source": "browser_dom",
                "label_confidence": LABEL_CONFIDENCE.get(label, 0.8),
                "card_tag": item.get("card_tag"),
                "time_style": item.get("time_style"),
                "badges": item.get("badges") or [],
                "shelf": item.get("shelf"),
                "title_aria": item.get("title_aria"),
                "thumb_aspect": item.get("thumb_aspect"),
                "pp": item.get("pp"),
                "doc_top": doc_top,
                "section_index": item.get("section_index"),
                "stale": item.get("stale", False),
                "in_first_screen": (
                    doc_top is not None and viewport_height is not None and doc_top < viewport_height
                ),
                "evidence": {
                    "source": "browser_dom",
                    "notes": f"{item.get('card_tag')} card in the {page_kind} feed.",
                },
            }
        )
        if label == "normal_video":
            normal_count += 1
            if normal_count >= max(max_candidates, 1):
                break

    return results


def capture_selected_card(page: Any, page_kind: str, video_id: str, path: Path) -> str | None:
    try:
        marked = page.evaluate(FEED_CARDS_JS, {"mode": "mark", "pageKind": page_kind, "videoId": video_id})
        if not marked:
            return None
        page.wait_for_timeout(400)
        # Redraw the box once layout has settled after scrolling the card into view.
        page.evaluate(
            FEED_CARDS_JS,
            {"mode": "mark", "pageKind": page_kind, "videoId": video_id, "keepScroll": True},
        )
        page.wait_for_timeout(100)
        return take_screenshot(page, path)
    except Exception:
        return None
    finally:
        try:
            page.evaluate(FEED_CARDS_JS, {"mode": "unmark", "pageKind": page_kind})
        except Exception:
            pass


def new_navigation_record(selected: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": None,
        "requested_video_id": selected["video_id"],
        "requested_url": selected.get("url"),
        "landed_video_id": None,
        "landed_url": None,
        "fallback_reason": None,
        "tap_landed_url": None,
        "related_refreshed": None,
        "reloaded": False,
        "elapsed_ms": None,
        "error": None,
    }


def navigate_to_selected(
    page: Any,
    selected: dict[str, Any],
    page_kind: str,
    settings: BrowserObservationSettings,
    timeout_error: type[Exception],
    record: dict[str, Any],
) -> dict[str, Any]:
    """Open the selected video the way a phone user would (a tap), falling back to page.goto.

    Fills ``record`` in place, so it survives on the round even if navigation fails.
    """
    video_id = selected["video_id"]
    started = time.monotonic()
    try:
        previous_ids: list[str] = []
        if page_kind == "watch":
            previous_ids = page.evaluate(FEED_CARDS_JS, {"mode": "ids", "pageKind": "watch", "limit": 3})
            page.evaluate(FEED_CARDS_JS, {"mode": "mark_stale", "pageKind": "watch"})

        if settings.navigation == "tap":
            try:
                tap_selected_card(page, page_kind, video_id, record)
                record["method"] = "tap"
            except Exception as exc:
                record["fallback_reason"] = error_summary(exc, 200)

        if record["method"] is None:
            page.goto(selected["url"], wait_until="domcontentloaded")
            record["method"] = "goto" if settings.navigation == "goto" else "goto_fallback"

        refreshed = wait_for_watch_ready(page, video_id, previous_ids, timeout_error)
        if not refreshed and video_id_from_url(page.url) != video_id:
            raise BrowserObservationError(
                f"Navigation landed on {video_id_from_url(page.url)!r} instead of {video_id!r}."
            )
        if not refreshed:
            # Never read the previous video's list as this one's: reload the page once.
            record["reloaded"] = True
            page.goto(selected["url"], wait_until="domcontentloaded")
            refreshed = wait_for_watch_ready(page, video_id, [], timeout_error)
        record["related_refreshed"] = refreshed
        if not refreshed:
            raise BrowserObservationError(f"The related list for {video_id!r} did not load.")

        record["landed_url"] = page.url
        record["landed_video_id"] = video_id_from_url(page.url)
        if record["landed_video_id"] != video_id:
            raise BrowserObservationError(
                f"Navigation landed on {record['landed_video_id']!r} instead of {video_id!r}."
            )
        return record
    except Exception as exc:
        record["error"] = error_summary(exc, 300)
        record["landed_url"] = record["landed_url"] or safe_page_url(page)
        raise
    finally:
        record["elapsed_ms"] = round((time.monotonic() - started) * 1000)


def tap_selected_card(page: Any, page_kind: str, video_id: str, record: dict[str, Any]) -> None:
    link = page.evaluate_handle(
        FEED_CARDS_JS,
        {"mode": "link", "pageKind": page_kind, "videoId": video_id},
    ).as_element()
    if link is None:
        raise BrowserObservationError("selected card is no longer in the feed")
    link.scroll_into_view_if_needed(timeout=3_000)
    before_url = page.url
    link.tap(timeout=5_000)
    # Wait for any URL change, then check it is the requested video, so a tap that opened
    # something else (another video, a Short, a channel) falls back at once and is recorded.
    try:
        page.wait_for_function("(before) => location.href !== before", arg=before_url, timeout=10_000)
    finally:
        if page.url != before_url:
            record["tap_landed_url"] = page.url
    if video_id_from_url(page.url) != video_id:
        raise BrowserObservationError(f"the tap opened {page.url!r} instead")
    record["tap_landed_url"] = None


def wait_for_watch_ready(
    page: Any,
    video_id: str | None,
    previous_ids: list[str],
    timeout_error: type[Exception],
    timeout_ms: int = 10_000,
) -> bool:
    """Wait until the URL shows the new video and the related list belongs to it.

    After a tap YouTube navigates in-page, so the old related list can linger briefly. The old
    cards were flagged stale before navigating; the list counts as refreshed once a card without
    that flag appears, or once the leading video ids change. Returns False if that never happened.
    A page that is reloading (e.g. after the cookie dialog) is simply waited out.
    """
    script = (
        "([vid, prev]) => {"
        "  try {"
        "    if (vid && new URL(location.href).searchParams.get('v') !== vid) return false;"
        f"    const feed = ({FEED_CARDS_JS});"
        "    const ids = feed({mode: 'ids', pageKind: 'watch', limit: 3});"
        "    if (!ids.length) return false;"
        "    if (!prev.length) return true;"
        "    return feed({mode: 'fresh_count', pageKind: 'watch'}) > 0 || ids.join() !== prev.join();"
        "  } catch (e) { return false; }"
        "}"
    )
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return False
        try:
            page.wait_for_function(script, arg=[video_id, previous_ids], timeout=remaining_ms)
            return True
        except timeout_error:
            return False
        except Exception:
            # "Execution context was destroyed": the page navigated mid-wait. Try again.
            page.wait_for_timeout(300)


def enrich_browser_results(
    results: list[dict[str, Any]],
    youtube_client: YouTubeClient | None,
) -> tuple[list[dict[str, Any]], str | None]:
    if youtube_client is None or not results:
        return results, None

    video_ids = [result["video_id"] for result in results if result.get("video_id")]
    try:
        metadata_by_id = youtube_client.video_metadata_by_id(video_ids)
    except Exception as exc:
        # Enrichment is optional; never let the API end a browser run.
        return results, error_summary(exc)

    enriched: list[dict[str, Any]] = []
    for result in results:
        video_id = result.get("video_id")
        metadata = metadata_by_id.get(video_id)
        if not metadata:
            enriched.append(result)
            continue

        merged = result.copy()
        observed = {
            "observed_title": result.get("title"),
            "observed_channel_name": result.get("channel_name"),
            "observed_url": result.get("url"),
        }
        merged.update(metadata)
        merged["position"] = result.get("position")
        merged["raw_position"] = result.get("raw_position", result.get("position"))
        merged["display_rank"] = result.get("display_rank")
        merged["label"] = result.get("label")
        merged["source"] = result.get("source")
        # Keep the URL that was observed (with YouTube's own pp/list context, and /shorts/ paths);
        # the API's canonical URL goes in canonical_url.
        merged["url"] = result.get("url")
        merged["canonical_url"] = canonical_watch_url(video_id)
        merged["metadata_source"] = "youtube_api"
        merged["browser_observation"] = observed
        merged["evidence"] = {
            "source": "youtube_api",
            "notes": "Video metadata enriched from YouTube Data API after browser observation.",
        }
        enriched.append(merged)

    return enriched, None


def excluded_ids_for_continuation(
    session: dict[str, Any],
    through_round: int | None = None,
) -> list[str]:
    """Video ids a continuation or branch should inherit, following the parent's exclude policy.

    A chain started from round ``through_round`` replaces that round's pick with its own start
    video (which the runner excludes itself), so that round's pick is only inherited if it was
    displayed. Under "watched" the branch never watched it, so it stays selectable.
    """
    policy = (session.get("settings") or {}).get("exclude_policy", "displayed")
    excluded: set[str] = set(session.get("initial_excluded_video_ids") or [])
    for round_record in session.get("rounds", []):
        round_number = round_record.get("round", 0)
        if through_round is not None and round_number > through_round:
            break
        if policy == "displayed":
            for item in round_record.get("raw_results") or round_record.get("results") or []:
                if item.get("video_id"):
                    excluded.add(item["video_id"])
        selected_id = (round_record.get("selected_result") or {}).get("video_id")
        if selected_id and (through_round is None or round_number < through_round):
            excluded.add(selected_id)
    return sorted(excluded)


def safe_page_url(page: Any) -> str | None:
    try:
        return page.url
    except Exception:
        return None


def video_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if "/shorts/" in parsed.path:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "shorts":
            return parts[1]
    query = parse_qs(parsed.query)
    values = query.get("v")
    if values:
        return values[0]
    return None


def canonical_watch_url(video_id: str) -> str:
    return f"https://m.youtube.com/watch?v={video_id}"


def ensure_mobile_url(url: str) -> str:
    parsed = urlparse(url)
    if "youtube.com" not in parsed.netloc:
        return url
    suffix = parsed.path
    if parsed.query:
        suffix += f"?{parsed.query}"
    return f"https://m.youtube.com{suffix}"


def parse_duration_text(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", value.strip())
    if not match:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def clean_title(value: str) -> str:
    text = " ".join(value.split())
    return text[:240]


def clean_optional_text(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(value.split())
    return text[:160] or None
