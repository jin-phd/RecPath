"""Shared helpers for the Streamlit pages: configuration, labels, formatting, persisted state."""
from __future__ import annotations

import os
import re
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import streamlit as st

from recpath.config import RuntimeConfig, parse_runtime_config
from recpath.safety import error_summary, register_secrets
from recpath.time_utils import DUBLIN_TZ


PAGE_NEW = "ui/pages/new_run.py"
PAGE_LIVE = "ui/pages/live_run.py"
PAGE_LIBRARY = "ui/pages/library.py"
PAGE_COMPARE = "ui/pages/compare.py"

STRATEGY_LABELS = {"top_1": "Top result", "random": "Random"}
CONSENT_LABELS = {"accept_all": "Accept all", "reject_all": "Reject all"}
LOCATION_LABELS = {"IE": "Ireland (IE)", "GB": "United Kingdom (GB)", "US": "United States (US)"}
NAVIGATION_LABELS = {"tap": "Tap the card", "goto": "Open URL directly"}
EXCLUDE_POLICY_LABELS = {"displayed": "Everything displayed", "watched": "Only selected videos"}
LABEL_NAMES = {
    "normal_video": "videos",
    "short": "Shorts",
    "ad": "ads",
    "live": "live",
    "upcoming": "upcoming",
    "movie": "movies",
    "playlist": "playlists",
    "mix": "mixes",
    "channel": "channels",
    "other": "other",
}
LABEL_SINGULAR = {
    "normal_video": "video",
    "short": "Short",
    "ad": "ad",
    "live": "live",
    "upcoming": "upcoming",
    "movie": "movie",
    "playlist": "playlist",
    "mix": "mix",
    "channel": "channel",
    "other": "other",
}
MODE_SHORT_LABELS = {"browser_observation": "Chain", "shorts_feed": "Shorts feed", "api_metadata": "API"}
NAVIGATION_DONE = {
    "tap": "tapped",
    "goto": "opened by URL",
    "goto_fallback": "opened by URL after the tap failed",
}
LABEL_COLORS = {
    "normal_video": "blue",
    "short": "violet",
    "ad": "orange",
    "live": "red",
    "upcoming": "gray",
    "movie": "gray",
    "playlist": "gray",
    "mix": "gray",
    "channel": "gray",
    "other": "gray",
}
STATUS_COLORS = {
    "completed": "green",
    "stopped": "blue",
    "running": "blue",
    "stopping": "blue",
    "error": "red",
    "interrupted": "orange",
    "unfinished": "gray",
    "legacy": "gray",
}
END_REASON_TEXT = {
    "max_rounds": "all planned rounds finished",
    "no_candidates": "no eligible video was left to select",
    "feed_not_found": "no recommendation list was found on the page",
    "stopped": "stopped on request",
    "error": "ended by an error",
    "interrupted": "interrupted",
    "feed_stuck": "the Shorts feed stopped advancing",
    "location_mismatch": "YouTube did not apply the requested content location",
}
SHORTS_END_REASON_TEXT = {
    "max_rounds": "all requested Shorts recorded",
    "no_candidates": "no Short was found to open",
    "feed_not_found": "no Short was found to open",
}
UNFINISHED_NOTE = (
    "This run never finished: the app was stopped or crashed while it was running, so the "
    "file still says \"running\". Everything saved before that is shown here."
)
STEP_TEXT = {
    "opening": "opening YouTube",
    "consent": "cookie dialog",
    "observing": "observing",
    "scrolling": "reading the feed",
    "navigating": "opening the next video",
    "shorts": "watching Shorts",
}
SECRETS_HINT = (
    "Copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml`, fill in the key, "
    "and restart the app (or set the environment variable of the same name)."
)


# ---------------------------------------------------------------------- configuration
def get_config() -> RuntimeConfig:
    # Streamlit copies root-level secrets.toml values into os.environ when it starts, replacing
    # variables of the same name, so the effective order is CLI > secrets.toml > environment.
    config = parse_runtime_config(sys.argv[1:], environ={**os.environ, **secrets_as_environ()})
    register_secrets([config.youtube_api_key, config.openai_api_key, config.gcp_credentials_path])
    return config


MAX_ROUNDS_LIMIT = 10  # the Rounds inputs' maximum


def default_rounds(config: RuntimeConfig) -> int:
    """--max-rounds as a Rounds input's starting value. Streamlit silently resets an
    out-of-range value to min_value, so 12 would become 1; it is clamped to 1..10 instead."""
    try:
        value = int(config.max_rounds)
    except (TypeError, ValueError):
        value = 5
    return min(max(value, 1), MAX_ROUNDS_LIMIT)


def secrets_as_environ() -> dict[str, str]:
    """Root-level values from .streamlit/secrets.toml, e.g. YOUTUBE_API_KEY = "..."."""
    try:
        loaded = st.secrets.load_if_toml_exists()
    except Exception as exc:
        st.warning(f"Could not read secrets.toml: {error_summary(exc)}")
        return {}
    if not loaded:
        warn_about_unreadable_secrets_files()
        return {}
    values = {key: str(value) for key, value in st.secrets.items() if isinstance(value, (str, int, float))}
    empty = sorted(key for key, value in values.items() if value == "")
    if empty:
        st.warning(
            f"{', '.join(empty)} is empty in secrets.toml. An empty entry also hides an "
            "environment variable of the same name; remove the line or fill it in."
        )
    return {key: value for key, value in values.items() if value}


def warn_about_unreadable_secrets_files() -> None:
    """Streamlit treats a malformed secrets.toml like a missing one; say so instead."""
    for name in st.config.get_option("secrets.files") or []:
        path = Path(name).expanduser()
        if not path.is_file():
            continue
        try:
            tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            st.warning(f"Could not read {path}: {error_summary(exc)}")


# ---------------------------------------------------------------------- persisted state
# Widget state with key= is dropped when st.navigation switches pages, so every value that must
# survive a page switch is mirrored into a plain session_state entry and seeded back from it.
def _store_key(name: str) -> str:
    return f"keep::{name}"


def widget_key(name: str) -> str:
    return f"w::{name}"


def remembered(name: str, default: Any = None) -> Any:
    return st.session_state.get(_store_key(name), default)


def remember(name: str, value: Any) -> None:
    st.session_state[_store_key(name)] = value


def set_widget_value(name: str, value: Any) -> None:
    """Change a persisted widget's value (call before the widget is drawn, e.g. in a callback)."""
    st.session_state[_store_key(name)] = value
    st.session_state[widget_key(name)] = value


def persistent(widget: Callable[..., Any], name: str, default: Any, *args: Any, **kwargs: Any) -> Any:
    """Draw ``widget(*args, key=..., **kwargs)`` with a value that survives page switches."""
    store = _store_key(name)
    key = widget_key(name)
    if store not in st.session_state:
        st.session_state[store] = default
    if key not in st.session_state:
        st.session_state[key] = st.session_state[store]
    value = widget(*args, key=key, **kwargs)
    st.session_state[store] = value
    return value


# ---------------------------------------------------------------------- formatting
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$:])")


def md(text: Any) -> str:
    """Escape text from YouTube so it renders literally inside st.markdown / labels."""
    if text is None:
        return ""
    value = " ".join(str(text).split())
    return _MARKDOWN_SPECIAL.sub(r"\\\1", value)


def sentence(text: Any) -> str:
    """Error text (often a Playwright locator with _ * [ ] < >) escaped for markdown, without a
    trailing full stop, so callers can end their own sentence with one."""
    return md(" ".join(str(text or "").split()).rstrip(". ") or "unknown error")


def error_place(error_round: Any, finished: int, unit: str = "round", *, leading: str = "") -> str:
    """Where a run failed: "in round 3" while that round was being read, or "after round 2"
    when round 2 was already saved (the failure came while opening the next video). "" if unknown."""
    if not isinstance(error_round, int) or error_round < 1:
        return ""
    if error_round <= finished:
        return f"{leading}after {unit} {error_round}"
    return f"{leading}in {unit} {error_round}"


# Runs that finished without error but ended before their planned length, and how loudly to say
# so: a missing feed may mean YouTube changed its layout (warning); the others are informational.
EARLY_END_ALERT = {"feed_not_found": "warning", "no_candidates": "info", "feed_stuck": "info"}


def early_end_alert(end_reason: Any) -> str | None:
    """"warning" / "info" for a run that ended early on a safeguard, None otherwise."""
    return EARLY_END_ALERT.get(end_reason) if isinstance(end_reason, str) else None


def end_reason_text(end_reason: Any, mode: Any = None) -> str:
    """Why a run ended, in words ("" if unknown). Shorts feed runs count Shorts, not rounds."""
    if not end_reason:
        return ""
    if mode == "shorts_feed" and end_reason in SHORTS_END_REASON_TEXT:
        return SHORTS_END_REASON_TEXT[end_reason]
    return END_REASON_TEXT.get(str(end_reason), str(end_reason).replace("_", " "))


def shorten(text: Any, limit: int = 80) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def label_name(label: str | None, count: int | None = None) -> str:
    """Readable card type: plural by default ("Shorts"), singular for a count of 1 ("1 ad")."""
    key = label or "other"
    if count == 1:
        return LABEL_SINGULAR.get(key, str(key))
    return LABEL_NAMES.get(key, str(key))


def type_name(label: str | None) -> str:
    """Singular card type for a table cell."""
    return LABEL_SINGULAR.get(label or "other", str(label or "other"))


def strategy_label(value: str | None) -> str:
    return STRATEGY_LABELS.get(value or "", str(value or "—"))


def compact_number(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    number = float(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= limit:
            return f"{number / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(number))


def duration_label(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return ""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def local_time(iso_value: Any, fmt: str = "%H:%M:%S") -> str:
    """UTC ISO timestamp (as the runner writes it) -> Dublin local time."""
    if not iso_value:
        return ""
    try:
        moment = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except ValueError:
        return str(iso_value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(DUBLIN_TZ).strftime(fmt)


def short_created(created_local: Any) -> str:
    """'2026-07-15 13:12:11 IST' -> '2026-07-15 13:12'."""
    value = str(created_local or "")
    return value[:16] if len(value) >= 16 else value


def source_label(source: Any) -> str:
    if source == "search_results":
        return "search"
    if source == "watch_recommendations":
        return "up next"
    return "API search" if source is None else str(source)


def session_title(summary: dict[str, Any]) -> str:
    return f"{short_created(summary.get('created_local'))} · {shorten(summary.get('query') or '(no query)', 48)}"
