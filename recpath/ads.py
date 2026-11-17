"""Ad observation for watch pages: what the player showed, what YouTube scheduled, and the
ad beacons the page sent. Observation only — the runner never clicks an ad or requests an ad URL.
"""
from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

PLAYER_STATE_JS = (Path(__file__).parent / "js" / "player_state.js").read_text(encoding="utf-8")

# Ad-related request URL fragments seen on m.youtube.com watch pages, counted per round.
BEACON_PATTERNS = {
    "stats_ads": "/api/stats/ads",
    "pagead_adview": "/pagead/adview",
    "activeview": "/pcs/activeview",
    "interaction": "/pagead/interaction",
}
POD_POSITION_RE = re.compile(r"(\d+)\s+of\s+(\d+)")
COUNTDOWN_RE = re.compile(r"\s*\d+:\d{2}\s*$")
AD_TEXT_KEYS = ("headline", "description", "title", "adTitle", "callToAction", "displayUrl", "advertiserName", "text")
MAX_AD_TEXTS = 8


def observe_player(
    page: Any,
    duration_ms: int,
    *,
    on_first_ad: Callable[[], str | None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    interval_ms: int = 500,
    screenshot_wait_ms: int = 1_000,
) -> dict[str, Any]:
    """Sample the player every ``interval_ms`` for ``duration_ms`` and summarise the ads seen.

    ``on_first_ad`` is called once an in-player ad has rendered (its label or title is on screen,
    or ``screenshot_wait_ms`` after the player switched to ad mode, so the frame is not a black
    player); its return value is stored as ``screenshot``. ``should_stop`` ends the watch early.
    """
    started = time.monotonic()
    samples = 0
    first_seen_ms: int | None = None
    screenshot_ms: int | None = None
    ads: list[dict[str, Any]] = []
    companion = None
    paid_promotion = False
    screenshot = None
    stopped_early = False
    last_state: dict[str, Any] = {}
    while True:
        try:
            state = page.evaluate(PLAYER_STATE_JS)
        except Exception:
            state = {}
        samples += 1
        last_state = state or last_state
        elapsed = round((time.monotonic() - started) * 1000)
        if state.get("ad_showing"):
            if first_seen_ms is None:
                first_seen_ms = elapsed
            pod_text = COUNTDOWN_RE.sub("", state.get("pod_text") or "") or None
            position = POD_POSITION_RE.search(pod_text or "")
            ad = {
                "title": state.get("ad_title"),
                "pod_text": pod_text,
                "pod_position": f"{position.group(1)} of {position.group(2)}" if position else None,
                "advertiser_host": state.get("advertiser_host"),
                "skippable": state.get("skippable"),
            }
            record_ad_sample(ads, ad)
            rendered = any(ad.get(key) for key in IDENTITY_KEYS)
            if (
                on_first_ad
                and screenshot_ms is None
                and (rendered or elapsed - first_seen_ms >= screenshot_wait_ms)
            ):
                screenshot_ms = elapsed
                try:
                    screenshot = on_first_ad()
                except Exception:
                    screenshot = None
        companion = state.get("companion") or companion
        paid_promotion = paid_promotion or bool(state.get("paid_promotion"))
        if elapsed >= duration_ms:
            break
        if should_stop and should_stop():
            stopped_early = True
            break
        page.wait_for_timeout(min(interval_ms, max(duration_ms - elapsed, 50)))
    return {
        "observed_ms": round((time.monotonic() - started) * 1000),
        "samples": samples,
        "stopped_early": stopped_early,
        "player": {
            "found": bool(last_state.get("player_found")),
            "ad_seen": first_seen_ms is not None,
            "first_seen_ms": first_seen_ms,
            "screenshot_ms": screenshot_ms,
            "ads": ads,
            "video_time_at_end": last_state.get("video_time"),
            "screenshot": screenshot,
        },
        "companion": companion,
        "paid_promotion": paid_promotion,
    }


IDENTITY_KEYS = ("title", "pod_text", "pod_position", "advertiser_host")


def record_ad_sample(ads: list[dict[str, Any]], ad: dict[str, Any]) -> None:
    """Add one player sample to the ads seen so far.

    Ads in a pod play one after another, so a sample belongs to the ad currently playing (the
    last one) unless it contradicts it: a different known title, advertiser or pod position.
    Samples taken before the label renders, or after YouTube shortens it to "Sponsored", carry
    no or partial identity and are merged into the current ad instead of counted as new ones.
    """
    if ads and same_ad(ad, ads[-1]):
        current = ads[-1]
        for key, value in ad.items():
            if current.get(key) in (None, False) and value:
                current[key] = value
        return
    ads.append(dict(ad))


def same_ad(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two samples can be the same ad: no identifying field known in both differs."""
    keys = ("title", "advertiser_host", "pod_position")
    return all(a.get(key) is None or b.get(key) is None or a.get(key) == b.get(key) for key in keys)


def distinct_player_ads(ads: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Ads of a recorded round, ignoring identity-less samples next to identified ones.

    Sessions recorded before Oct 22 2026 could store one ad twice: a sample without any text
    taken before the label rendered, plus the identified one.
    """
    ads = [ad for ad in ads or [] if isinstance(ad, dict)]
    identified = [ad for ad in ads if any(ad.get(key) for key in IDENTITY_KEYS)]
    return identified if identified else ads[:1]


def scheduled_ads(player_response: dict[str, Any] | None) -> tuple[list[dict[str, Any]], bool | None]:
    """Ads YouTube scheduled for a video, from its player response (adSlots / adPlacements).

    Returns (slots, paid_promotion). Tracking URLs are never kept, only slot kinds, offsets,
    ad video ids and short display texts.
    """
    if not player_response:
        return [], None
    slots: list[dict[str, Any]] = []
    for slot in player_response.get("adSlots") or []:
        renderer = slot.get("adSlotRenderer") or {}
        metadata = renderer.get("adSlotMetadata") or {}
        slots.append(
            {
                "kind": slot_kind(metadata.get("slotType"), metadata.get("triggerEvent")),
                "slot_type": metadata.get("slotType"),
                "trigger": metadata.get("triggerEvent"),
                "offset_ms": None,
                "ad_video_ids": sorted(set(collect_values(renderer, "externalVideoId"))),
                "texts": ad_texts(renderer),
            }
        )
    for placement in player_response.get("adPlacements") or []:
        config = ((placement.get("adPlacementRenderer") or {}).get("config") or {}).get("adPlacementConfig") or {}
        kind = config.get("kind")
        offset = (config.get("adTimeOffset") or {}).get("offsetStartMilliseconds")
        slots.append(
            {
                "kind": {
                    "AD_PLACEMENT_KIND_START": "preroll",
                    "AD_PLACEMENT_KIND_END": "postroll",
                    "AD_PLACEMENT_KIND_MILLISECONDS": "midroll",
                }.get(kind, "other"),
                "slot_type": kind,
                "trigger": None,
                "offset_ms": int(offset) if str(offset or "").lstrip("-").isdigit() else None,
                "ad_video_ids": sorted(set(collect_values(placement, "externalVideoId"))),
                "texts": ad_texts(placement),
            }
        )
    paid = "paidContentOverlay" in player_response if player_response else None
    return slots, paid


def slot_kind(slot_type: str | None, trigger: str | None) -> str:
    if slot_type == "SLOT_TYPE_PLAYER_BYTES":
        return "preroll" if trigger == "SLOT_TRIGGER_EVENT_BEFORE_CONTENT" else "in_stream"
    if slot_type == "SLOT_TYPE_BELOW_PLAYER":
        return "below_player"
    if slot_type == "SLOT_TYPE_IN_PLAYER":
        return "in_player"
    return "other"


def collect_values(node: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, (str, int)):
                found.append(v)
            else:
                found.extend(collect_values(v, key))
    elif isinstance(node, list):
        for v in node:
            found.extend(collect_values(v, key))
    return found


def ad_texts(node: Any) -> list[str]:
    """Short human-readable strings (advertiser, headline, call to action) from an ad renderer."""
    texts: list[str] = []

    def visit(value: Any, under_text_key: bool) -> None:
        if len(texts) >= MAX_AD_TEXTS:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                if k in ("simpleText", "content") and under_text_key and isinstance(v, str):
                    add(v)
                elif k == "runs" and under_text_key and isinstance(v, list):
                    add("".join(run.get("text", "") for run in v if isinstance(run, dict)))
                else:
                    visit(v, under_text_key or k in AD_TEXT_KEYS)
        elif isinstance(value, list):
            for v in value:
                visit(v, under_text_key)

    def add(text: str) -> None:
        text = " ".join(text.split())
        if text and len(text) <= 120 and "://" not in text and text not in texts:
            texts.append(text)

    visit(node, False)
    return texts


class AdBeaconCounter:
    """Counts ad-related requests the page makes (impression/view pings), per round."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def attach(self, context: Any) -> None:
        context.on("request", self.on_request)

    def on_request(self, request: Any) -> None:
        url = request.url
        for name, fragment in BEACON_PATTERNS.items():
            if fragment in url:
                self.counts[name] += 1

    def take(self) -> dict[str, int]:
        counts = {name: self.counts.get(name, 0) for name in BEACON_PATTERNS}
        self.counts.clear()
        return counts
