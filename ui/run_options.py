"""Run options chosen on the New run page, remembered for continuations and branches."""
from __future__ import annotations

from typing import Any

from ui.common import remembered
from recpath import run_manager
from recpath.config import RuntimeConfig


def default_dwell_seconds() -> float:
    value = run_manager.settings_field_default("watch_dwell_ms", 1_500)
    return round((value or 0) / 1000, 1)


def browser_options(config: RuntimeConfig) -> dict[str, Any]:
    """The New run page's current browser values (its defaults if it was never opened).

    Continuations and branches use these only for what the parent session does not decide:
    pacing, window visibility, API enrichment, and settings an older session did not record.
    """
    fields = run_manager.settings_field_names()
    enrich = bool(config.youtube_api_key) and bool(remembered("api_enrichment", True))
    options: dict[str, Any] = {
        "max_candidates": remembered("scan_depth", 20),
        "headless": not remembered("visible_window", False),
        "slow_mo_ms": remembered("pacing_ms", 250),
        "device": remembered("device", "Pixel 7"),
        "region": remembered("content_location", "IE"),
        "consent_choice": remembered("consent_choice", "accept_all"),
        "navigation": remembered("navigation", "tap"),
        "exclude_policy": remembered("exclude_policy", "displayed"),
        "youtube_api_key": config.youtube_api_key if enrich else None,
    }
    if "watch_dwell_ms" in fields:
        options["watch_dwell_ms"] = int(round(float(remembered("watch_dwell_s", default_dwell_seconds())) * 1000))
    for name in ("check_shorts_urls", "capture_innertube"):
        if name in fields:
            options[name] = bool(remembered(name, run_manager.settings_field_default(name, False)))
    return options
