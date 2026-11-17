"""Live run: mirror the phone screen and the chain while a crawl runs in the background."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from ui.common import (
    CONSENT_LABELS,
    PAGE_LIBRARY,
    PAGE_NEW,
    STEP_TEXT,
    early_end_alert,
    end_reason_text,
    error_place,
    local_time,
    md,
    sentence,
    shorten,
    short_created,
    strategy_label,
)
from ui.components import ads_tile, chain_cards, metric_row, phone_frame, shorts_filmstrip, status_dot, thumb_bytes
from recpath import library, run_manager
from recpath.library import MODE_LABELS
from recpath.paths import project_relative
from recpath.storage import sessions_to_json


LIBRARY_SELECTION = "library_selected"


def open_in_library(session_id: str | None) -> None:
    if session_id:
        st.session_state[LIBRARY_SELECTION] = session_id
    st.switch_page(PAGE_LIBRARY)


# ---------------------------------------------------------------------- derived state
def run_state(snap: dict[str, Any]) -> dict[str, Any]:
    events = snap["events"]
    params = snap["params"]
    rounds = [event["record"] for event in events if event.get("type") == "round" and isinstance(event.get("record"), dict)]
    shorts = [event["item"] for event in events if event.get("type") == "short" and isinstance(event.get("item"), dict)]
    steps = [event for event in events if event.get("type") == "step"]
    last_step = steps[-1] if steps else {}
    screenshots = [event.get("screenshot") for event in steps if event.get("screenshot")]
    kind = snap["kind"]
    planned = params.get("items") if kind == run_manager.KIND_SHORTS else params.get("max_rounds")
    done = len(shorts) if kind == run_manager.KIND_SHORTS else len(rounds)
    current_round = last_step.get("round") or (len(rounds) + 1 if snap["finished_at"] is None else len(rounds))
    return {
        "events": events,
        "params": params,
        "rounds": rounds,
        "shorts": shorts,
        "last_step": last_step,
        "latest_screenshot": screenshots[-1] if screenshots else None,
        "planned": planned if isinstance(planned, int) and planned > 0 else None,
        "done": done,
        "current_round": current_round,
    }


def event_detail(event: dict[str, Any], mode: str | None = None) -> str:
    kind = event.get("type")
    if kind == "started":
        return f"Started “{event.get('start_query') or ''}”"
    if kind == "step":
        return str(event.get("message") or STEP_TEXT.get(event.get("step"), event.get("step") or ""))
    if kind == "round":
        record = event.get("record") or {}
        selected = library.selected_of(record)
        counts = library.label_counts(record)
        text = f"{record.get('raw_result_count') or 0} cards ({counts.get('normal_video', 0)} videos, {counts.get('short', 0)} Shorts)"
        if selected:
            text += f"; picked “{shorten(selected.get('title') or selected.get('video_id'), 60)}”"
        else:
            text += "; nothing left to pick"
        return text
    if kind == "navigation":
        navigation = event.get("navigation") or {}
        text = f"Opened by {navigation.get('method') or '—'}"
        if navigation.get("elapsed_ms") is not None:
            text += f" in {navigation['elapsed_ms'] / 1000:.1f} s"
        if navigation.get("error"):
            text += f" · {navigation['error']}"
        return text
    if kind == "short":
        item = event.get("item") or {}
        if item.get("is_ad"):
            ad = item.get("ad") or {}
            return f"Short #{item.get('position')}: ad {ad.get('advertiser') or ad.get('headline') or ''}".strip()
        return f"Short #{item.get('position')}: {shorten(item.get('title') or item.get('video_id'), 70)}"
    if kind == "finished":
        error = event.get("error") or {}
        text = f"Finished: {event.get('status')} ({end_reason_text(event.get('end_reason'), mode)})"
        if error.get("message"):
            text += f" · {error['message']}"
        return text
    return ""


def event_log(events: list[dict[str, Any]], mode: str | None = None) -> None:
    rows = [
        {
            "time": local_time(event.get("at")),
            "event": event.get("type"),
            "round": str(event["round"]) if isinstance(event.get("round"), int) else "",
            "detail": event_detail(event, mode),
        }
        for event in events[-200:]
    ]
    if not rows:
        st.caption("No events yet.")
        return
    st.dataframe(
        pd.DataFrame(rows[::-1]),
        hide_index=True,
        width="stretch",
        height=260,
        column_config={
            "time": st.column_config.TextColumn("Time", width="small"),
            "event": st.column_config.TextColumn("Event", width="small"),
            "round": st.column_config.TextColumn("Round", width="small"),
            "detail": st.column_config.TextColumn("Detail", width="large"),
        },
    )


STOP_HELP = (
    "Stop ends the run within a few seconds: the video being watched is cut short and its "
    "list is still read and saved. Everything recorded is kept."
)


# ---------------------------------------------------------------------- views
def _request_stop() -> None:
    # A button callback runs before the rerun, so the redrawn panel already says "Stopping…".
    # (No toast here: elements drawn from a fragment's callback land at the top of the page.)
    run_manager.request_stop()


def render_run(snap: dict[str, Any], *, live: bool) -> None:
    state = run_state(snap)
    params = state["params"]
    kind = snap["kind"]
    outcome = snap["outcome"]
    shorts_mode = kind == run_manager.KIND_SHORTS

    # Header row first, so progress and Stop stay at the top when the columns stack on a phone.
    query = params.get("start_query") or ""
    st.markdown(f"#### {md(shorten(query, 90)) or '(no query)'}")
    bits = [MODE_LABELS.get(kind, kind)]
    if not shorts_mode and params.get("strategy"):
        bits.append(f"strategy {strategy_label(params.get('strategy'))}")
    if params.get("device"):
        bits.append(str(params["device"]))
    if params.get("consent_choice"):
        bits.append(f"cookies: {CONSENT_LABELS.get(params['consent_choice'], params['consent_choice'])}")
    settings = params.get("settings") or {}
    if settings.get("max_candidates") and kind == run_manager.KIND_CHAIN:
        bits.append(f"scan depth {settings['max_candidates']}")
    if params.get("parent"):
        parent = params["parent"]
        relation = "continuation of" if parent.get("kind") == "continuation" else "branch of"
        bits.append(f"{relation} R{parent.get('round')} “{shorten(parent.get('title') or parent.get('video_id'), 40)}”")
    st.caption(" · ".join(md(bit) for bit in bits))

    planned, done = state["planned"], state["done"]
    unit = "Shorts" if shorts_mode else "rounds"
    if live:
        progress_text = (
            f"{done} of {planned} {unit} done" if planned else f"{done} {unit} done"
        ) + f" · {STEP_TEXT.get(state['last_step'].get('step'), 'starting')}"
    else:
        progress_text = f"{done} of {planned} {unit}" if planned else f"{done} {unit}"
    progress_value = min(done / planned, 1.0) if planned else (1.0 if not live else 0.0)
    if live:
        progress_col, stop_col = st.columns([4, 1], vertical_alignment="bottom")
        with progress_col:
            st.progress(progress_value, text=progress_text)
        with stop_col:
            st.button(
                "Stopping…" if snap["stop_requested"] else "Stop",
                icon=":material/stop_circle:",
                disabled=snap["stop_requested"],
                width="stretch",
                key="live_stop",
                on_click=_request_stop,
                help=STOP_HELP,
            )
        st.caption(STOP_HELP if not snap["stop_requested"] else "Stop requested: the run ends within a few seconds.")
    else:
        st.progress(progress_value, text=progress_text)

    left, right = st.columns([1, 2.3], gap="large")

    with left:
        if kind == run_manager.KIND_API:
            with st.container(border=True):
                st.markdown("**No browser in this mode**")
                st.caption("API metadata runs query the YouTube Data API directly; rounds appear on the right as they finish.")
        else:
            frame = snap["frame"]
            if frame is None and state["latest_screenshot"]:
                frame = thumb_bytes(library.resolve_screenshot(state["latest_screenshot"]), 520)
            step = state["last_step"]
            step_text = STEP_TEXT.get(step.get("step"), step.get("step") or "starting")
            if state["current_round"]:
                round_text = f"Short {state['current_round']}" if shorts_mode else f"round {state['current_round']}"
            else:
                round_text = "starting"
            if live:
                label = "STOPPING" if snap["stop_requested"] else "LIVE"
                caption = f'<span class="vt-live">{status_dot("live")}{label} · {round_text} · {step_text}</span>'
                if snap["frame_at"]:
                    caption += f'<br><span style="color:#8a94a3">frame {max(time.time() - snap["frame_at"], 0):.1f} s ago</span>'
            else:
                dot = {"completed": "done", "stopped": "stopped", "error": "error"}.get(outcome, "idle")
                caption = f"{status_dot(dot)}{outcome} · last frame"
            phone_frame(frame, caption_html=caption, width=290,
                        placeholder="Launching Chromium…" if live else "No frame was captured.")

    with right:
        rounds = state["rounds"]
        normal = sum(library.label_counts(record).get("normal_video", 0) for record in rounds)
        shorts_seen = sum(int(record.get("shorts_result_count") or 0) for record in rounds) + len(
            [item for item in state["shorts"] if not item.get("is_ad")]
        )
        ad_values = [library.round_ad_count(record) for record in rounds]
        ads_known = [value for value in ad_values if value is not None]
        ads = sum(ads_known) + len([item for item in state["shorts"] if item.get("is_ad")]) if (ads_known or state["shorts"]) else None
        if shorts_mode:
            ads_scope = library.ADS_SCOPE_SHORTS
        elif any(library.player_observed(record) for record in rounds):
            ads_scope = library.ADS_SCOPE_FEED_AND_PLAYER
        else:
            ads_scope = library.ADS_SCOPE_FEED_ONLY if ads_known else None
        tiles = [
            ("Rounds done" if not shorts_mode else "Shorts recorded",
             f"{done} / {planned}" if planned else done, None),
        ]
        if not shorts_mode:
            tiles.append(("Normal candidates", normal, "Normal videos read from the feeds so far"))
        tiles.append(("Shorts seen", shorts_seen, "Shorts cards in the feeds (or Shorts watched in Shorts feed mode)"))
        if kind != run_manager.KIND_API:
            tiles.append(ads_tile(ads, ads_scope))
        metric_row(tiles)

        if shorts_mode:
            st.markdown("**Shorts so far**")
            shorts_filmstrip(state["shorts"], card_width=130, limit=12)
        else:
            st.markdown("**Chain so far**")
            chain_cards(rounds, active_round=state["current_round"] if live else None, width=180)

        with st.expander("Event log", icon=":material/list_alt:"):
            event_log(state["events"], library.MODE_SHORTS if shorts_mode else None)


@st.fragment(run_every=1.0)
def live_panel(run_id: str) -> None:
    handle = run_manager.get(run_id)
    if handle is None or handle.done:
        st.rerun()
    render_run(handle.snapshot(), live=True)


def render_outcome(snap: dict[str, Any]) -> None:
    session = snap["session"] or {}
    outcome = snap["outcome"]
    shorts_mode = snap["kind"] == run_manager.KIND_SHORTS
    rounds = library.rounds_of(session)
    params = snap["params"] or {}
    planned = params.get("items") if shorts_mode else params.get("max_rounds")
    done = len(library.shorts_items(session)) if shorts_mode else len(rounds)
    unit = "Shorts" if shorts_mode else "rounds"
    count = f"{done} of {planned} {unit}" if isinstance(planned, int) and planned > 0 else f"{done} {unit}"
    reason = end_reason_text(session.get("end_reason"), library.MODE_SHORTS if shorts_mode else session.get("mode"))
    early = early_end_alert(session.get("end_reason"))
    if outcome == "completed" and early:
        alert = st.warning if early == "warning" else st.info
        alert(f"Ended early: {count} · {reason}.", icon=":material/warning:" if early == "warning" else ":material/info:")
    elif outcome == "completed":
        st.success(f"Completed: {count}" + (f" · {reason}." if reason else "."), icon=":material/check_circle:")
    elif outcome == "stopped":
        st.info(f"Stopped on request: {count} were finished and saved.", icon=":material/stop_circle:")
    elif outcome == "interrupted":
        st.warning(f"The run was interrupted: {count} were kept.", icon=":material/warning:")
    else:
        error = session.get("error") if isinstance(session.get("error"), dict) else {}
        where = error_place(error.get("round"), done, "Short" if shorts_mode else "round", leading=" ")
        kept = f" {count} finished before it and were kept." if done else ""
        st.error(f"The run ended with an error{where}: {sentence(snap['failure_message'])}.{kept}", icon=":material/error:")
    save_errors = session.get("save_errors") if isinstance(session.get("save_errors"), list) else []
    if save_errors and session.get("last_save_ok") is False:
        last = save_errors[-1] if isinstance(save_errors[-1], dict) else {}
        st.warning(
            f"Saving to disk failed ({sentence(last.get('message'))}). "
            "Download the session's JSON below while the app is running to keep it."
        )


@st.fragment(run_every=2.0)
def watch_for_new_run(shown_run_id: str | None) -> None:
    """Redraw the page when a run starts elsewhere (another tab, the library) while this one
    shows the idle or finished view; only the running view refreshes itself otherwise."""
    active = run_manager.current()
    if active is not None and active.run_id != shown_run_id:
        st.rerun()


def render_finished(handle: run_manager.RunHandle) -> None:
    snap = handle.snapshot()
    render_outcome(snap)
    session_id = snap["session_id"]
    saved = bool(session_id) and library.load_session(session_id) is not None
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        if st.button("Open in library", type="primary", icon=":material/video_library:", disabled=not saved):
            open_in_library(session_id)
        st.page_link(PAGE_NEW, label="Start another run", icon=":material/add_circle:")
        if snap["session"]:
            st.download_button(
                "Download JSON",
                data=sessions_to_json([snap["session"]]),
                file_name=f"{session_id or 'session'}.json",
                mime="application/json",
                icon=":material/download:",
                on_click="ignore",
            )
    if snap["saved_paths"]:
        first = Path(snap["saved_paths"][0])
        names = " and ".join(f"`{Path(path).name}`" for path in snap["saved_paths"])
        st.caption(f"Saved as {names} in `{project_relative(first.parent)}`.")
    render_run(snap, live=False)


def render_idle() -> None:
    st.info("Nothing is running right now.", icon=":material/radio_button_unchecked:")
    st.page_link(PAGE_NEW, label="Start a new run", icon=":material/add_circle:")
    sessions = library.index()
    if not sessions:
        return
    latest = sessions[0]
    st.markdown("**Latest saved session**")
    with st.container(border=True):
        thumb_col, text_col = st.columns([1, 4], vertical_alignment="center")
        with thumb_col:
            if latest.get("first_thumbnail"):
                st.image(latest["first_thumbnail"], width="stretch")
        with text_col:
            st.markdown(f"**{md(latest.get('query') or '(no query)')}**")
            amount = (
                f"{latest.get('items')} Shorts" if latest.get("mode") == library.MODE_SHORTS
                else f"{latest.get('rounds')} round(s)"
            )
            status = library.display_status(latest.get("status"), latest.get("session_id"), None)
            st.caption(
                f"{short_created(latest.get('created_local'))} · {latest.get('mode_label')} · "
                f"{amount} · {status}"
            )
            if st.button("Open in library", icon=":material/video_library:"):
                open_in_library(latest["session_id"])


st.title("Live run")
handle = run_manager.latest()
if handle is None:
    render_idle()
    watch_for_new_run(None)
elif handle.running:
    live_panel(handle.run_id)
else:
    render_finished(handle)
    watch_for_new_run(handle.run_id)
