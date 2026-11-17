from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


DUBLIN_TZ = ZoneInfo("Europe/Dublin")


def timestamp_pair() -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(DUBLIN_TZ)
    return (
        now_utc.isoformat().replace("+00:00", "Z"),
        now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
    )


def session_id_from_time(prefix: str = "session") -> str:
    now_utc = datetime.now(timezone.utc)
    return f"{prefix}_{now_utc.strftime('%Y%m%d_%H%M%S_%f')}"
