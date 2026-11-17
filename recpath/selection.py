from __future__ import annotations

import random
import secrets
from typing import Any


DEFAULT_DISPLAY_LIMIT = 10


def filter_excluded_videos(
    results: list[dict[str, Any]],
    excluded_video_ids: set[str] | list[str] | tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[str]]:
    excluded = set(excluded_video_ids)
    filtered: list[dict[str, Any]] = []
    filtered_out: list[str] = []

    for result in results:
        video_id = result.get("video_id")
        if video_id and video_id in excluded:
            filtered_out.append(video_id)
            continue
        filtered.append(result)

    return filtered, filtered_out


def raw_position(result: dict[str, Any]) -> int | None:
    value = result.get("raw_position", result.get("position"))
    return value if isinstance(value, int) else None


def top_normal_videos(
    results: list[dict[str, Any]],
    limit: int = DEFAULT_DISPLAY_LIMIT,
) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    for display_rank, item in enumerate(
        [result for result in results if result.get("label") == "normal_video"][:limit],
        start=1,
    ):
        ranked = item.copy()
        ranked["display_rank"] = display_rank
        ranked["raw_position"] = raw_position(item)
        videos.append(ranked)
    return videos


def assign_display_ranks(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy results, numbering normal videos 1..n so every candidate (not only the Top-10) has a rank."""
    ranked: list[dict[str, Any]] = []
    display_rank = 0
    for item in results:
        copy = item.copy()
        if copy.get("label") == "normal_video":
            display_rank += 1
            copy["display_rank"] = display_rank
        copy["raw_position"] = raw_position(item)
        ranked.append(copy)
    return ranked


def select_next_video(
    normal_candidates: list[dict[str, Any]],
    strategy: str,
    seed: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not normal_candidates:
        return None, {
            "strategy": strategy,
            "candidate_count": 0,
            "selected_video_id": None,
            "selected_display_rank": None,
            "selected_raw_position": None,
            "selected_position": None,
            "selected_title": None,
            "reason": "No normal video candidates were available.",
        }

    if strategy == "random":
        random_seed = seed if seed is not None else secrets.randbelow(1_000_000_000)
        rng = random.Random(random_seed)
        selected = rng.choice(normal_candidates)
        return selected, {
            "strategy": strategy,
            "random_seed": random_seed,
            "candidate_count": len(normal_candidates),
            "selected_video_id": selected.get("video_id"),
            "selected_display_rank": selected.get("display_rank"),
            "selected_raw_position": raw_position(selected),
            "selected_position": raw_position(selected),
            "selected_title": selected.get("title"),
            "reason": "Randomly selected from normal video candidates.",
        }

    selected = normal_candidates[0]
    return selected, {
        "strategy": "top_1",
        "candidate_count": len(normal_candidates),
        "selected_video_id": selected.get("video_id"),
        "selected_display_rank": selected.get("display_rank"),
        "selected_raw_position": raw_position(selected),
        "selected_position": raw_position(selected),
        "selected_title": selected.get("title"),
        "reason": "Selected the first normal video candidate.",
    }
