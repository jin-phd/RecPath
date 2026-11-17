from __future__ import annotations

import csv
import json
from io import StringIO
from pathlib import Path
from typing import Any

from .ads import distinct_player_ads
from .paths import SESSIONS_DIR


DATA_DIR = SESSIONS_DIR


def save_session(session: dict[str, Any], data_dir: Path = DATA_DIR) -> tuple[Path, Path]:
    data_dir.mkdir(parents=True, exist_ok=True)
    session_id = session["session_id"]
    json_path = data_dir / f"{session_id}.json"
    csv_path = data_dir / f"{session_id}.csv"
    write_text_atomic(json_path, json.dumps(session, indent=2, ensure_ascii=False))
    write_text_atomic(csv_path, session_to_csv(session))
    return json_path, csv_path


def write_text_atomic(path: Path, text: str) -> None:
    # Sessions are rewritten after every round; never leave a half-written file behind.
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def sessions_to_json(sessions: list[dict[str, Any]]) -> str:
    return json.dumps(sessions, indent=2, ensure_ascii=False)


def sessions_to_csv(sessions: list[dict[str, Any]]) -> str:
    rows: list[dict[str, Any]] = []
    for session in sessions:
        rows.extend(flatten_session_rows(session))
    return rows_to_csv(rows)


def session_to_csv(session: dict[str, Any]) -> str:
    return rows_to_csv(flatten_session_rows(session))


def flatten_session_rows(session: dict[str, Any]) -> list[dict[str, Any]]:
    if session.get("mode") == "shorts_feed":
        return flatten_shorts_feed_rows(session)
    rows: list[dict[str, Any]] = []
    for round_record in session.get("rounds", []):
        selected_id = (round_record.get("selected_result") or {}).get("video_id")
        selection = round_record.get("selection") or {}
        display_rank_by_id = {
            item.get("video_id"): item.get("display_rank")
            for item in [*round_record.get("results", []), *round_record.get("top_normal_videos", [])]
            if item.get("video_id") and item.get("display_rank")
        }
        excluded_ids = set(round_record.get("filtered_out_video_ids") or [])
        settings = session.get("settings") or {}
        navigation = round_record.get("navigation") or {}
        ads = round_record.get("ads") or {}
        player_ads = ads.get("player") or {}
        # Export the whole observed feed (raw_results) when available, flagging the items that
        # were excluded as already seen, so the observed ranking can be rebuilt from the CSV.
        for result in round_record.get("raw_results") or round_record.get("results", []):
            video_id = result.get("video_id")
            rows.append(
                {
                    "session_id": session.get("session_id"),
                    "mode": session.get("mode", "api_metadata"),
                    "round": round_record.get("round"),
                    "source": round_record.get("source"),
                    "page_url": round_record.get("page_url"),
                    "entry_type": round_record.get("entry_type"),
                    "entry_value": round_record.get("entry_value"),
                    "derived_query": round_record.get("derived_query"),
                    "query_source": round_record.get("query_source"),
                    "region": round_record.get("region") or session.get("region"),
                    "language": round_record.get("language") or session.get("language"),
                    "location_applied": (round_record.get("location") or {}).get("gl"),
                    "display_rank": result.get("display_rank") or display_rank_by_id.get(video_id),
                    "raw_position": result.get("raw_position", result.get("position")),
                    "label": result.get("label"),
                    "title": result.get("title"),
                    "url": result.get("url"),
                    "video_id": video_id,
                    "channel_name": result.get("channel_name"),
                    "channel_id": result.get("channel_id"),
                    "published_at": result.get("published_at"),
                    "duration_seconds": result.get("duration_seconds"),
                    "view_count": result.get("view_count"),
                    "like_count": result.get("like_count"),
                    "comment_count": result.get("comment_count"),
                    "metadata_source": result.get("metadata_source"),
                    "is_selected": bool(video_id) and video_id == selected_id,
                    "selection_strategy": selection.get("strategy"),
                    "label_confidence": result.get("label_confidence"),
                    "fetched_at": round_record.get("fetched_at"),
                    "fetched_at_local": round_record.get("fetched_at_local"),
                    "screenshot_path": round_record.get("screenshot_path"),
                    "raw_result_count": round_record.get("raw_result_count"),
                    "filtered_result_count": round_record.get("filtered_result_count"),
                    "filtered_duplicate_count": round_record.get("filtered_duplicate_count"),
                    "schema_version": session.get("schema_version", 1),
                    "session_status": session.get("status"),
                    "start_query": session.get("start_query"),
                    "parent_session_id": (session.get("parent") or {}).get("session_id"),
                    "batch_id": (session.get("batch") or {}).get("batch_id"),
                    "device": settings.get("device"),
                    "consent_choice": settings.get("consent_choice"),
                    "exclude_policy": settings.get("exclude_policy"),
                    "excluded_as_seen": video_id in excluded_ids if video_id else False,
                    "card_tag": result.get("card_tag"),
                    "observed_url": result.get("observed_url"),
                    "canonical_url": result.get("canonical_url"),
                    "channel_url": result.get("channel_url"),
                    "duration_text": result.get("duration_text"),
                    "time_style": result.get("time_style"),
                    "doc_top": result.get("doc_top"),
                    "in_first_screen": result.get("in_first_screen"),
                    "category_id": result.get("category_id"),
                    "navigation_method": (
                        navigation.get("method") if video_id and video_id == selected_id else None
                    ),
                    "selected_screenshot_path": (round_record.get("screenshots") or {}).get("selected"),
                    "presentation": result.get("presentation"),
                    "shorts_evidence": ";".join(result.get("shorts_evidence") or []),
                    "duration_bucket": result.get("duration_bucket"),
                    "round_player_ad_seen": player_ads.get("ad_seen"),
                    "round_player_ads": len(distinct_player_ads(player_ads.get("ads"))) if player_ads else None,
                    "round_scheduled_ads": len(ads.get("scheduled") or []) if ads else None,
                    "round_feed_ads": ads.get("feed_ads"),
                    **session_outcome_columns(session),
                }
            )
    return rows


def session_outcome_columns(session: dict[str, Any]) -> dict[str, Any]:
    """How the run ended and how long it was meant to be, so a stopped or early-ended run can be
    told apart from a full one (session_status stays the recorded status)."""
    settings = session.get("settings") or {}
    planned = settings.get("shorts_items") if session.get("mode") == "shorts_feed" else settings.get("max_rounds")
    return {"session_end_reason": session.get("end_reason"), "session_planned": planned}


def flatten_shorts_feed_rows(session: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per Short watched in a Shorts feed session (same columns as chain rows)."""
    settings = session.get("settings") or {}
    rows = []
    for item in session.get("shorts_feed") or []:
        ad = item.get("ad") or {}
        rows.append(
            {
                "session_id": session.get("session_id"),
                "mode": "shorts_feed",
                "round": item.get("position"),
                "source": "shorts_feed",
                "page_url": item.get("url"),
                "raw_position": item.get("position"),
                "label": "ad" if item.get("is_ad") else "short",
                "title": item.get("title") or ad.get("headline"),
                "url": item.get("url"),
                "video_id": item.get("video_id"),
                "channel_name": item.get("channel_name") or ad.get("advertiser"),
                "channel_id": (item.get("innertube") or {}).get("channel_id"),
                "metadata_source": "browser_dom",
                "is_selected": False,
                "fetched_at": item.get("at"),
                "screenshot_path": item.get("screenshot"),
                "region": session.get("region"),
                "language": session.get("language"),
                "schema_version": session.get("schema_version", 1),
                "session_status": session.get("status"),
                "start_query": session.get("start_query"),
                "device": settings.get("device"),
                "consent_choice": settings.get("consent_choice"),
                "presentation": "shorts_feed",
                **session_outcome_columns(session),
            }
        )
    return rows


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    fieldnames = [
        "session_id",
        "mode",
        "round",
        "source",
        "page_url",
        "entry_type",
        "entry_value",
        "derived_query",
        "query_source",
        "region",
        "language",
        "location_applied",
        "display_rank",
        "raw_position",
        "label",
        "title",
        "url",
        "video_id",
        "channel_name",
        "channel_id",
        "published_at",
        "duration_seconds",
        "view_count",
        "like_count",
        "comment_count",
        "metadata_source",
        "is_selected",
        "selection_strategy",
        "label_confidence",
        "fetched_at",
        "fetched_at_local",
        "screenshot_path",
        "raw_result_count",
        "filtered_result_count",
        "filtered_duplicate_count",
        "schema_version",
        "session_status",
        "start_query",
        "parent_session_id",
        "batch_id",
        "device",
        "consent_choice",
        "exclude_policy",
        "excluded_as_seen",
        "card_tag",
        "observed_url",
        "canonical_url",
        "channel_url",
        "duration_text",
        "time_style",
        "doc_top",
        "in_first_screen",
        "category_id",
        "navigation_method",
        "selected_screenshot_path",
        "presentation",
        "shorts_evidence",
        "duration_bucket",
        "round_player_ad_seen",
        "round_player_ads",
        "round_scheduled_ads",
        "round_feed_ads",
        "session_end_reason",
        "session_planned",
    ]
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()
