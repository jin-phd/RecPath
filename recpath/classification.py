from __future__ import annotations

import re
from typing import Any


ISO_DURATION_RE = re.compile(
    r"^P"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?"
    r")?$"
)


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_iso8601_duration(duration: str | None) -> int | None:
    if not duration:
        return None
    match = ISO_DURATION_RE.match(duration)
    if not match:
        return None
    parts = {key: int(value or 0) for key, value in match.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def classify_result(result: dict[str, Any]) -> tuple[str, float, dict[str, Any]]:
    raw_type = result.get("raw_type")
    duration_seconds = result.get("duration_seconds")
    live_broadcast_content = result.get("live_broadcast_content")

    if raw_type != "video":
        return (
            "other",
            0.9,
            {"source": "youtube_api", "notes": f"Raw type is {raw_type!r}."},
        )

    if live_broadcast_content in {"live", "upcoming"}:
        return (
            "other",
            0.85,
            {
                "source": "youtube_api",
                "notes": f"Live broadcast content is {live_broadcast_content!r}.",
            },
        )

    # The Data API has no Shorts flag. Since Oct 2024 a Short can last up to 3 minutes, so
    # duration is only a hint (browser mode checks YouTube's /shorts/<id> answer instead).
    if duration_seconds is not None and duration_seconds <= 60:
        return (
            "short",
            0.85,
            {"source": "youtube_api", "notes": "Duration is 60 seconds or less."},
        )
    if duration_seconds is not None and duration_seconds <= 180:
        return (
            "normal_video",
            0.6,
            {
                "source": "youtube_api",
                "notes": "61-180 seconds: may be a Short (up to 3 minutes since Oct 2024).",
            },
        )

    if duration_seconds is None:
        return (
            "other",
            0.6,
            {"source": "youtube_api", "notes": "Duration is unavailable."},
        )

    return (
        "normal_video",
        0.9,
        {"source": "youtube_api", "notes": "Playable video longer than 60 seconds."},
    )
