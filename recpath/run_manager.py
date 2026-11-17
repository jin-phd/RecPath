"""Process-wide registry of crawl runs, so a UI can start one and watch it without blocking.

Only one crawl may run per process: it drives a real browser on the user's machine. A run
executes the runner in a daemon worker thread (sync Playwright lives entirely inside that
thread) and collects what the runner reports through its hooks:

- ``events``: the runner's progress events, deep-copied when they arrive,
- ``frame``: the latest JPEG of the phone screen and when it arrived,
- ``session``: the final session dict, or ``error`` (an ``error_summary``) if the runner raised.
  When the runner raises after a checkpoint, the last checkpointed session is marked
  ``status="error"``, saved once more and kept as ``session``, so its rounds are not shown
  (or left on disk) as still "running".

``request_stop()`` sets a ``threading.Event`` that the runner polls through ``should_stop``; the
run then ends after its current step. Nothing here imports Streamlit.
"""
from __future__ import annotations

import copy
import dataclasses
import itertools
import threading
import time
from collections import deque
from typing import Any, Callable

from . import api_tracking, browser_observation, library, storage
from .safety import error_summary
from .time_utils import timestamp_pair


KIND_CHAIN = "browser_observation"
KIND_SHORTS = "shorts_feed"
KIND_API = "api_metadata"
MAX_EVENTS = 2_000
RECENT_LIMIT = 20

_LOCK = threading.Lock()
_ACTIVE: "RunHandle | None" = None
_RECENT: deque["RunHandle"] = deque(maxlen=RECENT_LIMIT)
_COUNTER = itertools.count(1)


class RunManagerError(RuntimeError):
    pass


class RunAlreadyActive(RunManagerError):
    """Raised when a run is started while another one is still active."""

    def __init__(self, active: "RunHandle") -> None:
        query = active.params.get("start_query") or "a run"
        super().__init__(
            f"A run is already in progress ({query!r}, started {active.started_label()}). "
            "Wait for it to finish or stop it first."
        )
        self.active = active


class RunHandle:
    """One crawl run. All mutable state is guarded by ``_lock``; read it through ``snapshot()``."""

    def __init__(self, kind: str, params: dict[str, Any]) -> None:
        self.run_id = f"run_{next(_COUNTER)}_{int(time.time())}"
        self.kind = kind
        self.params = params  # public, display-only parameters (never keys)
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.stop_event = threading.Event()
        self.stop_requested_at: float | None = None
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._dropped_events = 0
        self._frame: bytes | None = None
        self._frame_at: float | None = None
        self._frame_count = 0
        self._session_id: str | None = None
        self._checkpoint_count = 0
        self._saved_paths: list[str] = []
        self._save_error: str | None = None
        self._last_session: dict[str, Any] | None = None  # the runner's dict at its last checkpoint
        self.session: dict[str, Any] | None = None
        self.error: str | None = None

    # ------------------------------------------------------------------ hooks (worker thread)
    def _on_event(self, event: dict[str, Any]) -> None:
        # Deep-copy now, in the worker thread: the runner keeps mutating the round record later.
        try:
            item = copy.deepcopy(event)
        except Exception:
            item = dict(event)
        item.setdefault("received_at", time.time())
        with self._lock:
            if item.get("session_id"):
                self._session_id = item["session_id"]
            self._events.append(item)
            if len(self._events) > MAX_EVENTS:
                # Keep the first events (started) and the newest ones.
                del self._events[1:2]
                self._dropped_events += 1

    def _on_frame(self, data: bytes) -> None:
        with self._lock:
            self._frame = bytes(data)
            self._frame_at = time.time()
            self._frame_count += 1

    def _checkpoint(self, target: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
        def checkpoint(session: dict[str, Any]) -> Any:
            with self._lock:
                self._session_id = session.get("session_id") or self._session_id
                self._last_session = session
            result = target(session)  # errors propagate to the runner, which records them
            with self._lock:
                self._checkpoint_count += 1
                if isinstance(result, tuple):
                    self._saved_paths = [str(path) for path in result]
            return result

        return checkpoint

    def _round_in_progress(self) -> int | None:
        """The round of the newest progress step (the round a crash happened in), if known."""
        with self._lock:
            events = list(self._events)
        for event in reversed(events):
            if event.get("type") == "step" and isinstance(event.get("round"), int):
                return event["round"]
        return None

    def _close_crashed_session(
        self, message: str, target: Callable[[dict[str, Any]], Any]
    ) -> dict[str, Any] | None:
        """After the runner raised: mark its last checkpointed session as an error and save it.

        Without this the file on disk would keep status "running" for good. Returns that
        session (with the rounds finished before the crash), or None if nothing was saved yet.
        """
        with self._lock:
            session = self._last_session
        if not isinstance(session, dict):
            return None
        if session.get("status") in (None, "running"):
            rounds = session.get("rounds") if isinstance(session.get("rounds"), list) else []
            session["status"] = "error"
            session["end_reason"] = "error"
            session["error"] = {
                "round": self._round_in_progress() or len(rounds) or None,
                "message": message,
                "page_url": None,
            }
            session["finished_at"] = timestamp_pair()[0]
            try:
                result = target(session)
            except Exception as exc:  # the crash is reported either way; note the failed save too
                session.setdefault("save_errors", []).append({"at": timestamp_pair()[0], "message": error_summary(exc)})
                session["last_save_ok"] = False
            else:
                with self._lock:
                    self._checkpoint_count += 1
                    if isinstance(result, tuple):
                        self._saved_paths = [str(path) for path in result]
        return session

    def _finish(self, *, session: dict[str, Any] | None = None, error: str | None = None) -> None:
        with self._lock:
            self.session = session
            self.error = error
            if session and session.get("session_id"):
                self._session_id = session["session_id"]
            self.finished_at = time.time()

    # ------------------------------------------------------------------ reading (any thread)
    @property
    def session_id(self) -> str | None:
        with self._lock:
            return self._session_id

    @property
    def done(self) -> bool:
        return self.finished_at is not None

    @property
    def running(self) -> bool:
        return not self.done

    @property
    def stop_requested(self) -> bool:
        return self.stop_event.is_set()

    def outcome(self) -> str:
        """running | stopping | completed | stopped | error | interrupted."""
        if not self.done:
            return "stopping" if self.stop_requested else "running"
        session = self.session or {}
        status = session.get("status")
        # The runner saves "interrupted" and then re-raises the interrupt, which also sets error.
        if status == "interrupted":
            return "interrupted"
        if self.error:
            return "error"
        if status == "error":
            return "error"
        if status == "interrupted":
            return "interrupted"
        if session.get("end_reason") == "stopped":
            return "stopped"
        return "completed"

    def failure_message(self) -> str | None:
        if self.error:
            return self.error
        session = self.session or {}
        if session.get("status") == "error":
            return (session.get("error") or {}).get("message") or "The run ended with an error."
        return None

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

    def latest_frame(self) -> tuple[bytes | None, float | None]:
        with self._lock:
            return self._frame, self._frame_at

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id,
                "kind": self.kind,
                "params": dict(self.params),
                "session_id": self._session_id,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "stop_requested": self.stop_event.is_set(),
                "events": list(self._events),
                "dropped_events": self._dropped_events,
                "frame": self._frame,
                "frame_at": self._frame_at,
                "frame_count": self._frame_count,
                "checkpoints": self._checkpoint_count,
                "saved_paths": list(self._saved_paths),
                "session": self.session,
                "error": self.error,
                "outcome": self.outcome(),
                "failure_message": self.failure_message(),
            }

    def started_label(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.started_at))

    def join(self, timeout: float | None = None) -> bool:
        if self.thread is not None:
            self.thread.join(timeout)
        return self.done


# ---------------------------------------------------------------------- registry
def current() -> RunHandle | None:
    """The run that is still active, if any."""
    with _LOCK:
        return _ACTIVE


def latest() -> RunHandle | None:
    """The active run, or else the most recently started one in this process."""
    with _LOCK:
        if _ACTIVE is not None:
            return _ACTIVE
        return _RECENT[-1] if _RECENT else None


def recent() -> list[RunHandle]:
    """Runs started in this process, newest first."""
    with _LOCK:
        return list(reversed(_RECENT))


def get(run_id: str) -> RunHandle | None:
    with _LOCK:
        return next((handle for handle in _RECENT if handle.run_id == run_id), None)


def request_stop() -> bool:
    """Ask the active run to stop after its current step. Returns False if nothing is running."""
    handle = current()
    if handle is None or handle.done:
        return False
    if not handle.stop_event.is_set():
        handle.stop_requested_at = time.time()
        handle.stop_event.set()
    return True


def shorts_feed_available() -> bool:
    return callable(getattr(browser_observation, "run_shorts_feed", None))


def start_chain(**kwargs: Any) -> RunHandle:
    """Run ``run_browser_observation(**kwargs)`` in the background (checkpoint=save_session)."""
    settings = kwargs.get("settings")
    if settings is None:
        raise RunManagerError("start_chain needs settings=BrowserObservationSettings(...).")
    params = _public_params(KIND_CHAIN, kwargs, settings)
    return _start(KIND_CHAIN, params, lambda: browser_observation.run_browser_observation, kwargs)


def start_shorts_feed(**kwargs: Any) -> RunHandle:
    """Run ``run_shorts_feed(**kwargs)`` in the background, if this runner version has it."""
    if not shorts_feed_available():
        raise RunManagerError(
            "Shorts feed mode needs a browser runner with run_shorts_feed(); this version does not have it."
        )
    settings = kwargs.get("settings")
    if settings is None:
        raise RunManagerError("start_shorts_feed needs settings=BrowserObservationSettings(...).")
    params = _public_params(KIND_SHORTS, kwargs, settings)
    params["items"] = kwargs.get("items")
    return _start(KIND_SHORTS, params, lambda: getattr(browser_observation, "run_shorts_feed"), kwargs)


def start_api_chain(**kwargs: Any) -> RunHandle:
    """Run the YouTube Data API keyword chain (``api_tracking.run_api_chain``) in the background."""
    params = {
        "start_query": kwargs.get("entry_value") or kwargs.get("selected_query") or "",
        "strategy": kwargs.get("strategy"),
        "max_rounds": kwargs.get("max_rounds"),
        "max_results": kwargs.get("max_results"),
        "device": None,
        "parent": kwargs.get("parent"),
    }
    return _start(KIND_API, params, lambda: api_tracking.run_api_chain, kwargs)


def _public_params(kind: str, kwargs: dict[str, Any], settings: Any) -> dict[str, Any]:
    public = settings.public_dict() if hasattr(settings, "public_dict") else {}
    return {
        "start_query": kwargs.get("start_query") or "",
        "start_url": kwargs.get("start_url"),
        "parent": kwargs.get("parent"),
        "strategy": public.get("strategy"),
        "max_rounds": public.get("max_rounds"),
        "device": public.get("device"),
        "consent_choice": public.get("consent_choice"),
        "headless": public.get("headless"),
        "settings": public,
    }


def _start(
    kind: str,
    params: dict[str, Any],
    resolve_target: Callable[[], Callable[..., dict[str, Any]]],
    kwargs: dict[str, Any],
) -> RunHandle:
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is not None and not _ACTIVE.done:
            raise RunAlreadyActive(_ACTIVE)
        handle = RunHandle(kind, params)
        _ACTIVE = handle
        _RECENT.append(handle)

    call_kwargs = dict(kwargs)
    target_checkpoint = call_kwargs.pop("checkpoint", None) or storage.save_session
    call_kwargs["checkpoint"] = handle._checkpoint(target_checkpoint)
    call_kwargs["on_event"] = handle._on_event
    call_kwargs["should_stop"] = handle.stop_event.is_set
    call_kwargs["on_frame"] = handle._on_frame

    def work() -> None:
        global _ACTIVE
        try:
            target = resolve_target()
            session = target(**call_kwargs)
            handle._finish(session=session)
        except BaseException as exc:  # a crash must still release the slot and be reported
            message = error_summary(exc)
            session = None
            try:
                session = handle._close_crashed_session(message, target_checkpoint)
            except Exception:
                session = None
            handle._finish(session=session, error=message)
        finally:
            with _LOCK:
                if _ACTIVE is handle:
                    _ACTIVE = None

    thread = threading.Thread(target=work, name=f"recpath-{handle.run_id}", daemon=True)
    handle.thread = thread
    try:
        thread.start()
    except BaseException:
        with _LOCK:
            if _ACTIVE is handle:
                _ACTIVE = None
        raise
    return handle


# ---------------------------------------------------------------------- settings helpers
def settings_field_names() -> set[str]:
    return {item.name for item in dataclasses.fields(browser_observation.BrowserObservationSettings)}


def settings_field_default(name: str, fallback: Any = None) -> Any:
    for item in dataclasses.fields(browser_observation.BrowserObservationSettings):
        if item.name == name:
            if item.default is not dataclasses.MISSING:
                return item.default
            if item.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                return item.default_factory()  # type: ignore[misc]
    return fallback


def build_settings(**values: Any) -> Any:
    """BrowserObservationSettings from the values this runner version supports (others are dropped)."""
    names = settings_field_names()
    return browser_observation.BrowserObservationSettings(
        **{key: value for key, value in values.items() if key in names and value is not None}
    )


INHERITED_SETTINGS = (
    "max_candidates",
    "device",
    "consent_choice",
    "navigation",
    "exclude_policy",
    "max_scroll_passes",
    "watch_dwell_ms",
    "check_shorts_urls",
    "capture_innertube",
    "region",
    "location_cookie",
    "locale",
    "timezone_id",
)


def inherited_settings(session: dict[str, Any]) -> dict[str, Any]:
    """The measurement settings a continuation takes from ``session`` (see continuation_kwargs)."""
    recorded = session.get("settings") if isinstance(session.get("settings"), dict) else {}
    values = {name: recorded[name] for name in INHERITED_SETTINGS if recorded.get(name) is not None}
    if library.session_mode(session) == library.MODE_CHAIN and not recorded:
        # Sessions saved before settings were recorded: the runner then always opened videos
        # by URL and excluded every displayed video, and used one device profile.
        values.setdefault("navigation", "goto")
        values.setdefault("exclude_policy", "displayed")
    if "device" not in values and library.session_mode(session) == library.MODE_CHAIN:
        device = library.device_of(session)
        if device:
            values["device"] = device
    if library.session_mode(session) == library.MODE_CHAIN:
        # Sessions saved before the content location choice ran with the region at the top
        # level and never set the PREF cookie.
        if "region" not in values and session.get("region"):
            values["region"] = session["region"]
        values.setdefault("location_cookie", False)
    return values


def continuation_kwargs(
    session: dict[str, Any],
    *,
    start_result: dict[str, Any],
    parent_round: int | None,
    strategy: str,
    max_rounds: int,
    options: dict[str, Any],
    region: str = "IE",
    language: str = "en",
    default_system_prompt: str | None = None,
    default_openai_model: str | None = None,
) -> dict[str, Any]:
    """Arguments for start_chain() that continue or branch a browser session.

    Same rules as the original app: the measurement settings (device, consent, navigation,
    exclusion, scan depth) come from the parent session, and ``options`` (the New run page's
    current values) only fill in what an older session did not record. An older session's
    device is still known (``library.device_of``: its client record, or iPhone 13, the only
    profile before the choice was recorded), so a continued chain keeps its user agent.
    Rounds, pacing, window visibility and API enrichment come from ``options``. The content
    location (``region``) is a measurement setting too; ``region`` here is only the last
    fallback. The parent's excluded ids up to ``parent_round`` are inherited through
    ``excluded_ids_for_continuation``.
    """
    inherited = dict(inherited_settings(session))

    def setting(name: str, default: Any) -> Any:
        return inherited.get(name, options.get(name, default))

    values: dict[str, Any] = {
        "strategy": strategy,
        "max_rounds": max_rounds,
        "headless": options.get("headless", True),
        "slow_mo_ms": options.get("slow_mo_ms", 250),
        "youtube_api_key": options.get("youtube_api_key"),
        "region": region,
        "language": language,
    }
    defaults = {
        "region": region,
        "max_candidates": 20,
        "device": "Pixel 7",
        "consent_choice": "accept_all",
        "navigation": "tap",
        "exclude_policy": "displayed",
    }
    for name in INHERITED_SETTINGS:
        value = setting(name, defaults.get(name))
        if value is not None:
            values[name] = value
    return {
        "start_query": session.get("start_query") or start_result.get("title") or "",
        "generated_queries": session.get("generated_queries") or [],
        "selected_query": session.get("selected_query"),
        "settings": build_settings(**values),
        "system_prompt": session.get("system_prompt") or default_system_prompt,
        "user_prompt": session.get("user_prompt") or "",
        "openai_model": session.get("openai_model") or default_openai_model,
        "start_url": start_result.get("url") or start_result.get("observed_url") or start_result.get("canonical_url"),
        "parent": {
            "session_id": session.get("session_id"),
            "round": parent_round,
            "video_id": start_result.get("video_id"),
            "title": start_result.get("title"),
            "kind": parent_relation(session, parent_round, start_result.get("video_id")),
        },
        "initial_seen_ids": browser_observation.excluded_ids_for_continuation(
            session, through_round=parent_round
        ),
    }


def parent_relation(session: dict[str, Any], parent_round: int | None, video_id: str | None) -> str:
    """"continuation" when a chain resumes from a round's own pick, otherwise "branch"."""
    for round_record in library.rounds_of(session):
        if round_record.get("round") == parent_round:
            picked = (round_record.get("selected_result") or {}).get("video_id")
            return "continuation" if picked and picked == video_id else "branch"
    return "branch"


def _reset_for_tests() -> None:
    """Forget every run (tests only)."""
    global _ACTIVE
    with _LOCK:
        _ACTIVE = None
        _RECENT.clear()
