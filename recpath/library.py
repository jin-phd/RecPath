"""Index of the saved sessions in data/sessions, tolerant of every schema the app has written.

Schema-1 sessions (before Oct 2026) may lack schema_version, status, settings, consent, client,
screenshots and raw_results; API-metadata sessions may lack "mode". Summaries are cached by
file modification time, so re-indexing a folder of a few hundred sessions is cheap. Nothing
here imports Streamlit.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from . import paths
from .ads import distinct_player_ads


MODE_CHAIN = "browser_observation"
MODE_SHORTS = "shorts_feed"
MODE_API = "api_metadata"
MODE_LABELS = {
    MODE_CHAIN: "Recommendation chain",
    MODE_SHORTS: "Shorts feed",
    MODE_API: "API metadata",
}
LEGACY_STATUS = "legacy"
UNFINISHED_STATUS = "unfinished"
FULL_CACHE_SIZE = 48
# What an ad count covers: only ad cards labelled inside the feeds (the player was never
# observed), feed cards plus the player's ad record per round, or the Shorts player feed.
ADS_SCOPE_FEED_ONLY = "feed_only"
ADS_SCOPE_FEED_AND_PLAYER = "feed+player"
ADS_SCOPE_SHORTS = "shorts_feed"

_LOCK = threading.Lock()
_SUMMARY_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
_SESSION_CACHE: "OrderedDict[str, tuple[tuple[int, int], dict[str, Any]]]" = OrderedDict()


# ---------------------------------------------------------------------- small helpers
def thumbnail_url(video_id: str | None) -> str | None:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg" if video_id else None


def watch_url(video_id: str | None) -> str | None:
    return f"https://m.youtube.com/watch?v={video_id}" if video_id else None


def session_mode(session: dict[str, Any]) -> str:
    mode = session.get("mode")
    if mode in MODE_LABELS:
        return mode
    if session.get("shorts_feed") is not None and not session.get("rounds"):
        return MODE_SHORTS
    return MODE_API if not mode else str(mode)


def is_legacy(session: dict[str, Any]) -> bool:
    return not session.get("schema_version")


def session_status(session: dict[str, Any]) -> str:
    """The recorded status, or "legacy" for sessions saved before statuses were recorded."""
    status = session.get("status")
    if status:
        if status == "completed" and session.get("end_reason") == "stopped":
            return "stopped"
        return str(status)
    return LEGACY_STATUS


def display_status(status: str | None, session_id: str | None, active_session_id: str | None) -> str:
    """The status to show: a file that still says "running" while no run of this process is
    writing it was left behind by a crash or an app restart, so it is shown as "unfinished"."""
    value = str(status or LEGACY_STATUS)
    if value == "running" and (not session_id or session_id != active_session_id):
        return UNFINISHED_STATUS
    return value


def start_query(session: dict[str, Any]) -> str:
    return str(session.get("start_query") or session.get("selected_query") or "").strip()


def rounds_of(session: dict[str, Any]) -> list[dict[str, Any]]:
    rounds = session.get("rounds")
    return [item for item in rounds if isinstance(item, dict)] if isinstance(rounds, list) else []


def shorts_items(session: dict[str, Any]) -> list[dict[str, Any]]:
    items = session.get("shorts_feed")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def round_items(round_record: dict[str, Any]) -> list[dict[str, Any]]:
    """Every observed item of a round: raw_results when recorded (v2), else results (legacy)."""
    items = round_record.get("raw_results") or round_record.get("results") or []
    return [item for item in items if isinstance(item, dict)]


def label_counts(round_record: dict[str, Any]) -> dict[str, int]:
    counts = round_record.get("label_counts")
    if isinstance(counts, dict) and counts:
        return {str(key): int(value or 0) for key, value in counts.items() if key}
    computed: dict[str, int] = {}
    for item in round_items(round_record):
        label = item.get("label") or "other"
        computed[label] = computed.get(label, 0) + 1
    return computed


def selected_of(round_record: dict[str, Any]) -> dict[str, Any]:
    selected = round_record.get("selected_result")
    return selected if isinstance(selected, dict) else {}


def selected_position(round_record: dict[str, Any]) -> int | None:
    selection = round_record.get("selection") or {}
    selected = selected_of(round_record)
    for value in (
        selection.get("selected_raw_position"),
        selection.get("selected_position"),
        selected.get("raw_position"),
        selected.get("position"),
    ):
        if isinstance(value, int):
            return value
    return None


def device_of(session: dict[str, Any]) -> str | None:
    settings = session.get("settings") or {}
    client = session.get("client") or {}
    device = settings.get("device") or client.get("device")
    if device:
        return str(device)
    if session_mode(session) == MODE_CHAIN:
        return "iPhone 13"  # the only profile before device choice was recorded
    return None


def location_of(session: dict[str, Any]) -> str | None:
    """The content location (gl) a session asked for; sessions before the choice ran in IE."""
    settings = session.get("settings") or {}
    region = settings.get("region") or session.get("region")
    return str(region) if region else None


def consent_hit_wall(session: dict[str, Any]) -> bool:
    return any("consent.youtube.com" in str(item.get("page_url") or "") for item in rounds_of(session))


def consent_label(session: dict[str, Any]) -> str:
    """Short consent description: "Accept all", "Reject all · not applied", "not recorded"..."""
    consent = session.get("consent") or {}
    settings = session.get("settings") or {}
    choice = consent.get("choice") or settings.get("consent_choice")
    names = {"accept_all": "Accept all", "reject_all": "Reject all"}
    if session_mode(session) == MODE_API:
        return "n/a"
    if not choice:
        return "consent page" if consent_hit_wall(session) else "not recorded"
    label = names.get(choice, str(choice))
    if consent.get("events"):
        if consent.get("applied") is False:
            return f"{label} · not applied"
        return label
    if consent.get("checked") is False:
        return f"{label} · not checked"
    return f"{label} · no dialog"


def ads_of_round(round_record: dict[str, Any]) -> dict[str, Any] | None:
    ads = round_record.get("ads")
    return ads if isinstance(ads, dict) else None


def player_observed(round_record: dict[str, Any]) -> bool:
    """Whether the video player was watched for ads in this round (watch pages only)."""
    return isinstance((ads_of_round(round_record) or {}).get("player"), dict)


def round_ad_count(round_record: dict[str, Any]) -> int | None:
    """Ads observed in a round: feed ad cards plus player ads. None if nothing was recorded."""
    counts = round_record.get("label_counts")
    ads = ads_of_round(round_record)
    if ads is None and not isinstance(counts, dict):
        return None
    total = 0
    if isinstance(counts, dict):
        total += int(counts.get("ad") or 0)
    if ads:
        player = ads.get("player") or {}
        player_ads = distinct_player_ads(player.get("ads") if isinstance(player.get("ads"), list) else [])
        if player_ads:
            total += len(player_ads)
        elif player.get("ad_seen"):
            total += 1
        if not isinstance(counts, dict):
            total += int(ads.get("feed_ads") or 0)
    return total


def session_ad_count(session: dict[str, Any]) -> int | None:
    values = [round_ad_count(item) for item in rounds_of(session)]
    known = [value for value in values if value is not None]
    feed_ads = [item for item in shorts_items(session) if item.get("is_ad")]
    if not known and not shorts_items(session):
        return None
    return sum(known) + len(feed_ads)


def ads_scope(session: dict[str, Any]) -> str | None:
    """What session_ad_count() covers (one of the ADS_SCOPE_* values), None if it is unknown."""
    rounds = rounds_of(session)
    if any(player_observed(item) for item in rounds):
        return ADS_SCOPE_FEED_AND_PLAYER
    if session_mode(session) == MODE_SHORTS and shorts_items(session):
        return ADS_SCOPE_SHORTS
    if any(isinstance(item.get("label_counts"), dict) for item in rounds):
        return ADS_SCOPE_FEED_ONLY
    return None


def has_ad_data(session: dict[str, Any]) -> bool:
    return any(ads_of_round(item) is not None for item in rounds_of(session)) or any(
        "is_ad" in item for item in shorts_items(session)
    )


def resolve_screenshot(value: Any, screenshot_dir: Path | None = None) -> Path | None:
    """Find a saved screenshot: as stored, or by file name in the current screenshot folder.

    Paths are stored relative to the project root (data/screenshots/...) or absolute; the
    fallback finds them after the data folder was moved or RECPATH_DATA_DIR is set.
    """
    if not value or not isinstance(value, (str, Path)):
        return None
    candidate = paths.resolve_project_path(value)
    if candidate.is_file():
        return candidate
    folder = screenshot_dir or paths.SCREENSHOT_DIR
    fallback = Path(folder) / Path(str(value)).name
    return fallback if fallback.is_file() else None


def round_screenshots(round_record: dict[str, Any], screenshot_dir: Path | None = None) -> dict[str, Path]:
    """Existing screenshot files of a round by kind (landing, selected, ad)."""
    stored = round_record.get("screenshots") if isinstance(round_record.get("screenshots"), dict) else {}
    found: dict[str, Path] = {}
    for kind in ("landing", "selected", "ad"):
        path = resolve_screenshot(stored.get(kind), screenshot_dir)
        if path is None and kind == "landing":
            path = resolve_screenshot(round_record.get("screenshot_path"), screenshot_dir)
        if path is not None:
            found[kind] = path
    return found


def distinct_channels(session: dict[str, Any], *, picks_only: bool = False) -> int | None:
    names: set[str] = set()
    for round_record in rounds_of(session):
        items = [selected_of(round_record)] if picks_only else [
            item for item in round_items(round_record) if item.get("label") == "normal_video"
        ]
        for item in items:
            name = item.get("channel_id") or item.get("channel_name")
            if name:
                names.add(str(name))
    return len(names) if names else None


def recommended_ids(session: dict[str, Any], *, labels: Iterable[str] = ("normal_video",)) -> list[set[str]]:
    """Per round, the ids of the observed items with the given labels."""
    wanted = set(labels)
    return [
        {item["video_id"] for item in round_items(round_record) if item.get("video_id") and item.get("label") in wanted}
        for round_record in rounds_of(session)
    ]


def selected_ids(session: dict[str, Any]) -> list[str]:
    return [selected_of(item).get("video_id") for item in rounds_of(session) if selected_of(item).get("video_id")]


def export_safe(session: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``session`` the CSV/JSON exporters can always read.

    Older or hand-edited files can hold non-dict entries in "rounds" or in a round's item lists,
    or None where a list or dict is expected; one such file must not break a bulk export.
    The saved file is not changed.
    """
    clean = dict(session)
    rounds = []
    for record in rounds_of(session):
        record = dict(record)
        for name in ("results", "raw_results", "top_normal_videos", "shorts_results"):
            if name in record:
                value = record.get(name)
                record[name] = [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
        for name in ("selected_result", "selection", "navigation", "scroll", "ads", "screenshots"):
            if name in record and not isinstance(record.get(name), dict):
                record[name] = None
        for name in ("filtered_out_video_ids", "excluded_video_ids"):
            if name in record:
                value = record.get(name)
                record[name] = [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
        rounds.append(record)
    clean["rounds"] = rounds
    if "shorts_feed" in session:
        clean["shorts_feed"] = shorts_items(session)
    for name in ("settings", "consent", "client", "parent", "error"):
        if name in clean and not isinstance(clean.get(name), dict):
            clean[name] = None
    return clean


# ---------------------------------------------------------------------- summaries
def summarize(session: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    rounds = rounds_of(session)
    settings = session.get("settings") or {}
    picks = [selected_of(item) for item in rounds if selected_of(item)]
    first_pick = picks[0] if picks else {}
    last_pick = picks[-1] if picks else {}
    parent = session.get("parent") if isinstance(session.get("parent"), dict) else None
    mode = session_mode(session)
    shorts = sum(int(item.get("shorts_result_count") or 0) for item in rounds)
    feed = shorts_items(session)
    if mode == MODE_SHORTS:
        shorts += len([item for item in feed if not item.get("is_ad")])
    error = session.get("error") if isinstance(session.get("error"), dict) else {}
    normal = sum(label_counts(item).get("normal_video", 0) for item in rounds)
    first_thumb_id = first_pick.get("video_id") or next((item.get("video_id") for item in feed if item.get("video_id")), None)
    return {
        "session_id": str(session.get("session_id") or (path.stem if path else "")),
        "path": str(path) if path else None,
        "query": start_query(session),
        "created_at": str(session.get("created_at") or ""),
        "created_local": str(session.get("created_at_local") or session.get("created_at") or ""),
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, mode),
        "device": device_of(session),
        "location": location_of(session),
        "consent": consent_label(session),
        "consent_wall": consent_hit_wall(session),
        "strategy": session.get("strategy") or settings.get("strategy"),
        "rounds": len(rounds),
        "max_rounds": settings.get("max_rounds") if isinstance(settings.get("max_rounds"), int) else None,
        "items": len(feed),
        "status": session_status(session),
        "end_reason": session.get("end_reason"),
        "error_message": error.get("message"),
        "schema_version": session.get("schema_version") or 1,
        "shorts": shorts,
        "ads": session_ad_count(session),
        "ads_scope": ads_scope(session),
        "normal_candidates": normal,
        "first_selected_id": first_thumb_id,
        "first_thumbnail": thumbnail_url(first_thumb_id),
        "last_selected_title": last_pick.get("title") or "",
        "last_selected_id": last_pick.get("video_id"),
        "parent_session_id": parent.get("session_id") if parent else None,
        "parent_round": parent.get("round") if parent else None,
        "parent_kind": parent.get("kind") if parent else None,
        "is_branch": bool(parent and parent.get("session_id")),
        "api_enriched": any(item.get("metadata_enriched") for item in rounds) or mode == MODE_API,
    }


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sessions_dir_path(sessions_dir: Path | str | None = None) -> Path:
    return Path(sessions_dir) if sessions_dir else paths.SESSIONS_DIR


def session_files(sessions_dir: Path | str | None = None) -> list[Path]:
    folder = sessions_dir_path(sessions_dir)
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("*.json") if not path.name.startswith("."))


def index(sessions_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """Summaries of every readable session, newest first. Unreadable files are skipped."""
    summaries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for path in session_files(sessions_dir):
        key = _stat_key(path)
        if key is None:
            continue
        cache_key = str(path)
        seen_paths.add(cache_key)
        with _LOCK:
            cached = _SUMMARY_CACHE.get(cache_key)
        if cached and cached[0] == key:
            summaries.append(cached[1])
            continue
        session = _read_json(path)
        if session is None:
            continue
        summary = summarize(session, path)
        with _LOCK:
            _SUMMARY_CACHE[cache_key] = (key, summary)
        summaries.append(summary)
    with _LOCK:
        folder = str(sessions_dir_path(sessions_dir))
        for stale in [item for item in _SUMMARY_CACHE if item.startswith(folder) and item not in seen_paths]:
            _SUMMARY_CACHE.pop(stale, None)
            _SESSION_CACHE.pop(stale, None)
    summaries.sort(key=lambda item: (item["created_at"] or "", item["session_id"]), reverse=True)
    return summaries


def load_session(session_id: str, sessions_dir: Path | str | None = None) -> dict[str, Any] | None:
    if not session_id or "/" in session_id or "\\" in session_id or session_id.startswith("."):
        return None
    path = sessions_dir_path(sessions_dir) / f"{session_id}.json"
    key = _stat_key(path)
    if key is None:
        return None
    cache_key = str(path)
    with _LOCK:
        cached = _SESSION_CACHE.get(cache_key)
        if cached and cached[0] == key:
            _SESSION_CACHE.move_to_end(cache_key)
            return cached[1]
    session = _read_json(path)
    if session is None:
        return None
    with _LOCK:
        _SESSION_CACHE[cache_key] = (key, session)
        _SESSION_CACHE.move_to_end(cache_key)
        while len(_SESSION_CACHE) > FULL_CACHE_SIZE:
            _SESSION_CACHE.popitem(last=False)
    return session


def children_of(session_id: str, sessions_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """Summaries of the sessions continued or branched from ``session_id``, oldest first."""
    children = [item for item in index(sessions_dir) if item["parent_session_id"] == session_id]
    return sorted(children, key=lambda item: (item["created_at"], item["session_id"]))


def clear_cache() -> None:
    with _LOCK:
        _SUMMARY_CACHE.clear()
        _SESSION_CACHE.clear()
