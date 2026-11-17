"""Synthetic saved sessions in every schema the app has written, for library and UI tests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def item(video_id: str, position: int, label: str = "normal_video", **extra: Any) -> dict[str, Any]:
    return {
        "position": position,
        "raw_position": position,
        "display_rank": extra.pop("display_rank", None),
        "label": label,
        "title": extra.pop("title", f"Video *{video_id}* [{position}]"),
        "url": f"https://m.youtube.com/watch?v={video_id}",
        "video_id": video_id,
        "channel_name": extra.pop("channel_name", f"Channel {video_id[-1]}"),
        "metadata_source": "browser_dom",
        **extra,
    }


def browser_round(number: int, ids: list[str], *, shorts: int = 0, screenshots: dict | None = None,
                  legacy_shot: str | None = None, ads: dict | None = None, source: str | None = None) -> dict[str, Any]:
    results = [item(video_id, index + 1, display_rank=index + 1) for index, video_id in enumerate(ids)]
    results += [item(f"short{number}{index}", len(ids) + index + 1, "short") for index in range(shorts)]
    record: dict[str, Any] = {
        "round": number,
        "source": source or ("search_results" if number == 1 else "watch_recommendations"),
        "page_url": "https://m.youtube.com/results?search_query=x" if number == 1 else f"https://m.youtube.com/watch?v={ids[0]}",
        "fetched_at": f"2026-10-20T10:0{number}:00Z",
        "fetched_at_local": f"2026-10-20 11:0{number}:00 IST",
        "raw_result_count": len(results),
        "filtered_result_count": len(results),
        "filtered_duplicate_count": 0,
        "results": results,
        "top_normal_videos": [entry for entry in results if entry["label"] == "normal_video"][:10],
        "shorts_results": [entry for entry in results if entry["label"] == "short"],
        "shorts_result_count": shorts,
        "selected_result": results[0] if ids else None,
        "selection": {"strategy": "top_1", "selected_raw_position": 1 if ids else None,
                      "reason": "Selected the first normal video candidate."},
    }
    if legacy_shot:
        record["screenshot_path"] = legacy_shot
    if screenshots is not None:
        record["raw_results"] = results
        record["label_counts"] = {"normal_video": len(ids), **({"short": shorts} if shorts else {})}
        record["screenshots"] = screenshots
        record["screenshot_path"] = screenshots.get("landing")
        record["navigation"] = {"method": "tap", "requested_video_id": ids[0], "landed_video_id": ids[0]} if ids else None
    if ads is not None:
        record["ads"] = ads
    return record


def legacy_browser_session() -> dict[str, Any]:
    return {
        "session_id": "session_20260601_100000_000001",
        "created_at": "2026-06-01T09:00:00Z",
        "created_at_local": "2026-06-01 10:00:00 IST",
        "mode": "browser_observation",
        "start_query": "Peppa Pig # new $episodes$",
        "selected_query": "Peppa Pig # new $episodes$",
        "strategy": "top_1",
        "parent": None,
        "rounds": [
            browser_round(1, ["legacyA1", "legacyA2", "legacyA3"], shorts=2,
                          legacy_shot="data/screenshots/session_20260601_100000_000001_round_01.png"),
            browser_round(2, ["legacyB1", "legacyB2"],
                          legacy_shot="data/screenshots/session_20260601_100000_000001_round_02.png"),
        ],
    }


def legacy_api_session() -> dict[str, Any]:
    first = browser_round(1, ["apiA1", "apiA2"], source=None)
    first.pop("source")
    first.update({"entry_type": "query", "entry_value": "Irish teen gaming", "derived_query": "Irish teen gaming"})
    return {
        "session_id": "session_20260526_220000_000002",
        "created_at": "2026-05-26T21:00:00Z",
        "created_at_local": "2026-05-26 22:00:00 IST",
        "selected_query": "Irish teen gaming",
        "strategy": "random",
        "parent": None,
        "rounds": [first],
    }


def v2_session(screenshot_dir: Path) -> dict[str, Any]:
    shots = {}
    for number in (1, 2):
        for kind in ("landing", "selected"):
            path = screenshot_dir / f"v2_round_{number}_{kind}.jpg"
            shots[(number, kind)] = str(path)
    return {
        "schema_version": 2,
        "session_id": "session_20261020_120000_000003",
        "created_at": "2026-10-20T11:00:00Z",
        "created_at_local": "2026-10-20 12:00:00 IST",
        "finished_at": "2026-10-20T11:02:00Z",
        "mode": "browser_observation",
        "status": "completed",
        "end_reason": "max_rounds",
        "error": None,
        "start_query": "Transition Year vlog Ireland",
        "strategy": "top_1",
        "settings": {"strategy": "top_1", "max_rounds": 3, "max_candidates": 20, "device": "Pixel 7",
                     "consent_choice": "reject_all", "navigation": "tap", "exclude_policy": "watched",
                     "watch_dwell_ms": 1500},
        "client": {"device": "Pixel 7", "innertube_client": {"gl": "IE", "hl": "en"}},
        "consent": {"choice": "reject_all", "checked": True, "applied": True,
                    "events": [{"surface": "in_page_dialog", "ok": True}]},
        "parent": None,
        "initial_excluded_video_ids": ["inheritedZ"],
        "rounds": [
            browser_round(1, ["v2A1", "v2A2", "sharedX"], shorts=3,
                          screenshots={"landing": shots[(1, "landing")], "selected": shots[(1, "selected")]}),
            browser_round(2, ["v2B1", "sharedX", "v2B3"],
                          screenshots={"landing": shots[(2, "landing")], "selected": shots[(2, "selected")]}),
        ],
    }


def v3_ads_session() -> dict[str, Any]:
    session = {
        "schema_version": 3,
        "session_id": "session_20261021_090000_000004",
        "created_at": "2026-10-21T08:00:00Z",
        "created_at_local": "2026-10-21 09:00:00 IST",
        "mode": "browser_observation",
        "status": "completed",
        "end_reason": "stopped",
        "start_query": "Transition Year vlog Ireland",
        "strategy": "top_1",
        "settings": {"max_rounds": 5, "device": "iPhone 13", "consent_choice": "accept_all"},
        "consent": {"choice": "accept_all", "checked": True, "applied": True, "events": [{"ok": True}]},
        "parent": None,
        "rounds": [
            browser_round(1, ["v3A1", "sharedX"], screenshots={},
                          ads={"observed_ms": 0, "player": None, "companion": None, "scheduled": [], "feed_ads": 1}),
            browser_round(2, ["v3B1", "v3B2"], screenshots={},
                          ads={"observed_ms": 6000, "samples": 12,
                               "player": {"ad_seen": True, "first_seen_ms": 900,
                                          "ads": [{"title": "Introducing X", "pod_text": "Sponsored 1 of 2",
                                                   "advertiser_host": "x.ai", "skippable": True}]},
                               "companion": {"headline": "Try X", "cta_host": "x.ai"},
                               "scheduled": [{"kind": "midroll", "offset_ms": 90733}],
                               "paid_promotion": False, "feed_ads": 0}),
        ],
    }
    session["rounds"][0]["label_counts"]["ad"] = 1
    session["rounds"][0]["raw_results"].append(item("adcard1", 9, "ad", title="Sponsored: bank"))
    for record in session["rounds"]:
        for entry in record["raw_results"]:
            entry["presentation"] = "video_card"
    return session


def branch_session(parent: dict[str, Any]) -> dict[str, Any]:
    child = v2_session(Path("/nonexistent"))
    child["session_id"] = "session_20261020_130000_000005"
    child["created_at"] = "2026-10-20T12:00:00Z"
    child["created_at_local"] = "2026-10-20 13:00:00 IST"
    child["parent"] = {"session_id": parent["session_id"], "round": 2, "video_id": "v2B3", "title": "Video v2B3"}
    child["rounds"] = [browser_round(1, ["childA1", "childA2"], screenshots={}, source="watch_recommendations")]
    return child


def shorts_feed_session() -> dict[str, Any]:
    return {
        "schema_version": 3,
        "session_id": "session_20261021_100000_000006",
        "created_at": "2026-10-21T09:00:00Z",
        "created_at_local": "2026-10-21 10:00:00 IST",
        "mode": "shorts_feed",
        "status": "completed",
        "end_reason": "max_rounds",
        "start_query": "irish shorts",
        "settings": {"device": "Pixel 7", "consent_choice": "accept_all"},
        "consent": {"choice": "accept_all", "events": [{"ok": True}], "applied": True},
        "parent": None,
        "rounds": [],
        "shorts_feed": [
            {"position": 1, "video_id": "shortA", "url": "https://m.youtube.com/shorts/shortA", "title": "First",
             "channel_name": "C1", "is_ad": False, "ad": None, "screenshot": None, "dwell_ms": 6000},
            {"position": 2, "video_id": None, "url": "https://m.youtube.com/shorts/", "title": None,
             "channel_name": None, "is_ad": True, "ad": {"advertiser": "Bank", "headline": "Open now", "cta_host": "bank.ie"},
             "screenshot": None, "dwell_ms": 6000},
            {"position": 3, "video_id": "shortB", "url": "https://m.youtube.com/shorts/shortB", "title": "Third",
             "channel_name": "C2", "is_ad": False, "ad": None, "screenshot": None, "dwell_ms": 5500},
        ],
    }


def orphaned_running_session() -> dict[str, Any]:
    """A file a crash or an app restart left behind: status still "running", one round saved."""
    session = v2_session(Path("/nonexistent"))
    session.update(
        session_id="session_20261019_080000_000007",
        created_at="2026-10-19T07:00:00Z",
        created_at_local="2026-10-19 08:00:00 IST",
        finished_at=None,
        status="running",
        end_reason=None,
        start_query="orphaned run",
    )
    session["rounds"] = [browser_round(1, ["orphanA1", "orphanA2"], screenshots={})]
    return session


def write_sample_data(data_dir: Path) -> dict[str, dict[str, Any]]:
    """Write every synthetic session (plus screenshots and one corrupt file) under data_dir."""
    sessions_dir = data_dir / "sessions"
    screenshot_dir = data_dir / "screenshots"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    jpeg = _tiny_jpeg()
    for name in ("session_20260601_100000_000001_round_01.png", "session_20260601_100000_000001_round_02.png"):
        (screenshot_dir / name).write_bytes(_tiny_png())
    v2 = v2_session(screenshot_dir)
    for record in v2["rounds"]:
        for path in record["screenshots"].values():
            Path(path).write_bytes(jpeg)
    sessions = {
        "legacy_browser": legacy_browser_session(),
        "legacy_api": legacy_api_session(),
        "v2": v2,
        "v3_ads": v3_ads_session(),
        "branch": branch_session(v2),
        "shorts": shorts_feed_session(),
        "orphaned": orphaned_running_session(),
    }
    for session in sessions.values():
        (sessions_dir / f"{session['session_id']}.json").write_text(json.dumps(session), encoding="utf-8")
    (sessions_dir / "session_broken.json").write_text("{not json", encoding="utf-8")
    (sessions_dir / ".session_x.json.tmp").write_text("{}", encoding="utf-8")
    return sessions


def _tiny_png() -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (20, 40), (200, 30, 30)).save(buffer, "PNG")
    return buffer.getvalue()


def _tiny_jpeg() -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (20, 40), (30, 30, 200)).save(buffer, "JPEG")
    return buffer.getvalue()
