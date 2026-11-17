"""API metadata mode: a keyword chain through the YouTube Data API (the secondary mode).

Moved out of app.py so it can run in a background worker like the browser runner. The
behaviour is the original one: round 1 searches the start query, every later round searches
keywords derived locally from the previous pick (tags, title, description), Shorts and
already-seen videos are filtered out, and the next video is picked by strategy. It is not a
recommendation feed: the Data API has no related-videos lookup any more.
"""
from __future__ import annotations

from typing import Any, Callable

from .browser_observation import SCHEMA_VERSION
from .prompts import SYSTEM_PROMPT
from .safety import error_summary
from .selection import DEFAULT_DISPLAY_LIMIT, filter_excluded_videos, select_next_video, top_normal_videos
from .time_utils import session_id_from_time, timestamp_pair
from .youtube import YouTubeClient, derive_query_from_video


def make_session(
    *,
    user_prompt: str,
    generated_queries: list[dict[str, Any]],
    selected_query: str | None,
    strategy: str,
    region: str,
    language: str,
    system_prompt: str,
    openai_model: str | None,
    parent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created_at, created_at_local = timestamp_pair()
    return {
        "session_id": session_id_from_time(),
        "created_at": created_at,
        "created_at_local": created_at_local,
        "finished_at": None,
        "mode": "api_metadata",
        "task_type": "real_time",
        "status": "running",
        "end_reason": None,
        "error": None,
        "start_query": selected_query,
        "system_prompt": system_prompt,
        "openai_model": openai_model,
        "user_prompt": user_prompt,
        "region": region,
        "language": language,
        "generated_queries": generated_queries,
        "selected_query": selected_query,
        "strategy": strategy,
        "parent": parent,
        "rounds": [],
    }


def fetch_round(
    *,
    client: YouTubeClient,
    round_number: int,
    entry_type: str,
    entry_value: str,
    strategy: str,
    max_results: int,
    region: str,
    language: str,
    excluded_video_ids: set[str],
    entry_video: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    fetched_at, fetched_at_local = timestamp_pair()
    derived_query = entry_value
    query_source = "user_selected_query" if entry_type == "query" else "entry_value"
    query_reason = ""

    if entry_type == "video" and entry_video:
        derived_query = derive_query_from_video(entry_video) or entry_value
        query_source = "local_keyword_from_video"
        query_reason = "Derived locally from selected video tags, title, or description."

    bundle = client.search_videos(derived_query, max_results=max_results)
    raw_results = bundle["results"]
    results, filtered_out_video_ids = filter_excluded_videos(raw_results, excluded_video_ids)
    normal_candidates = [item for item in results if item.get("label") == "normal_video"]
    selected, selection = select_next_video(normal_candidates, strategy)
    top_videos = top_normal_videos(results, limit=DEFAULT_DISPLAY_LIMIT)
    shorts = [item for item in results if item.get("label") == "short"]
    others = [item for item in results if item.get("label") == "other"]

    round_record = {
        "round": round_number,
        "entry_type": entry_type,
        "entry_value": entry_value,
        "entry_video_id": entry_video.get("video_id") if entry_video else None,
        "derived_query": derived_query,
        "query_source": query_source,
        "query_reason": query_reason,
        "query_error": None,
        "fetched_at": fetched_at,
        "fetched_at_local": fetched_at_local,
        "region": region,
        "language": language,
        "raw_search_response": bundle["raw_search_response"],
        "raw_videos_response": bundle["raw_videos_response"],
        "raw_results": raw_results,
        "raw_result_count": len(raw_results),
        "filtered_result_count": len(results),
        "filtered_duplicate_count": len(filtered_out_video_ids),
        "excluded_video_ids": sorted(excluded_video_ids),
        "filtered_out_video_ids": filtered_out_video_ids,
        "results": results,
        "top_normal_videos": top_videos,
        "shorts_results": shorts,
        "shorts_result_count": len(shorts),
        "other_results": others,
        "other_result_count": len(others),
        "selected_result": selected,
        "selection": selection,
    }
    return round_record, selected


def run_api_chain(
    *,
    youtube_api_key: str,
    entry_value: str,
    strategy: str = "top_1",
    max_rounds: int = 5,
    max_results: int = 50,
    region: str = "IE",
    language: str = "en",
    user_prompt: str = "",
    generated_queries: list[dict[str, Any]] | None = None,
    selected_query: str | None = None,
    system_prompt: str | None = None,
    openai_model: str | None = None,
    entry_type: str = "query",
    entry_video: dict[str, Any] | None = None,
    parent: dict[str, Any] | None = None,
    client: YouTubeClient | None = None,
    checkpoint: Callable[[dict[str, Any]], Any] | None = None,
    on_event: Callable[[dict[str, Any]], Any] | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_frame: Callable[[bytes], Any] | None = None,  # accepted for a uniform signature; no frames
) -> dict[str, Any]:
    """Run an API keyword chain; same hooks and status fields as the browser runner.

    An API error ends the chain with ``status="error"`` and keeps the finished rounds.
    """
    client = client or YouTubeClient(api_key=youtube_api_key or "", region=region, language=language)
    session = make_session(
        user_prompt=user_prompt,
        generated_queries=generated_queries or [],
        selected_query=selected_query or (entry_value if entry_type == "query" else None),
        strategy=strategy,
        region=region,
        language=language,
        system_prompt=system_prompt or SYSTEM_PROMPT,
        openai_model=openai_model,
        parent=parent,
    )
    if not session["start_query"]:
        session["start_query"] = entry_value
    # Same schema generation as browser sessions, so the library does not treat it as legacy.
    session["schema_version"] = SCHEMA_VERSION
    session["settings"] = {
        "strategy": strategy,
        "max_rounds": max_rounds,
        "max_results": max_results,
        "region": region,
        "language": language,
        "youtube_api_enrichment": True,
    }

    def emit(event_type: str, **payload: Any) -> None:
        if not on_event:
            return
        try:
            on_event({"type": event_type, "session_id": session["session_id"], "at": timestamp_pair()[0], **payload})
        except Exception:
            pass

    def stop_requested() -> bool:
        try:
            return bool(should_stop and should_stop())
        except Exception:
            return False

    def save() -> None:
        if not checkpoint:
            return
        try:
            checkpoint(session)
            session["last_save_ok"] = True
        except Exception as exc:
            session["last_save_ok"] = False
            session.setdefault("save_errors", []).append(
                {"rounds_saved": len(session["rounds"]), "message": error_summary(exc)}
            )

    emit("started", settings={"strategy": strategy, "max_rounds": max_rounds, "max_results": max_results},
         start_query=session["start_query"], start_url=None)
    current_type, current_value, current_video = entry_type, entry_value, entry_video
    seen_video_ids: set[str] = set()
    if current_video and current_video.get("video_id"):
        seen_video_ids.add(current_video["video_id"])

    round_number = 0
    try:
        for round_number in range(1, max_rounds + 1):
            if stop_requested():
                session["end_reason"] = "stopped"
                break
            emit("step", round=round_number, step="observing",
                 message=f"Searching the YouTube Data API for round {round_number}")
            round_record, selected = fetch_round(
                client=client,
                round_number=round_number,
                entry_type=current_type,
                entry_value=current_value,
                strategy=strategy,
                max_results=max_results,
                region=region,
                language=language,
                excluded_video_ids=seen_video_ids,
                entry_video=current_video,
            )
            session["rounds"].append(round_record)
            emit("round", round=round_number, record=round_record)
            for result in round_record.get("raw_results", []):
                if result.get("video_id"):
                    seen_video_ids.add(result["video_id"])
            save()
            if not selected:
                session["end_reason"] = "no_candidates"
                break
            current_type = "video"
            current_value = selected.get("title") or selected.get("video_id") or ""
            current_video = selected
        session["end_reason"] = session["end_reason"] or "max_rounds"
        session["status"] = "completed"
    except Exception as exc:
        session["status"] = "error"
        session["end_reason"] = "error"
        session["error"] = {"round": round_number or None, "message": error_summary(exc), "page_url": None}
    finally:
        session["finished_at"] = timestamp_pair()[0]
        save()
    emit("finished", status=session["status"], end_reason=session["end_reason"], error=session["error"])
    return session
