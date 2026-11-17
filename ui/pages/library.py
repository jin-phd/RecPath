"""Session library: every saved session, with its timeline, path graph, candidates and ads."""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from ui.common import (
    CONSENT_LABELS,
    EXCLUDE_POLICY_LABELS,
    MODE_SHORT_LABELS,
    NAVIGATION_DONE,
    NAVIGATION_LABELS,
    PAGE_LIVE,
    PAGE_NEW,
    STATUS_COLORS,
    STRATEGY_LABELS,
    UNFINISHED_NOTE,
    default_rounds,
    end_reason_text,
    error_place,
    get_config,
    label_name,
    md,
    persistent,
    remembered,
    sentence,
    short_created,
    shorten,
    source_label,
    strategy_label,
)
from ui.components import (
    CANDIDATE_COLUMN_CONFIG,
    SHORTS_COLUMN_CONFIG,
    ads_panel,
    ads_tile,
    candidate_columns,
    candidates_frame,
    chain_dot,
    label_chips,
    metric_row,
    round_facts,
    round_record_without_items,
    shorts_filmstrip,
    shorts_frame,
    thumb_bytes,
)
from ui.run_options import browser_options
from recpath import library, paths, run_manager
from recpath.library import MODE_API, MODE_CHAIN, MODE_LABELS, MODE_SHORTS
from recpath.prompts import SYSTEM_PROMPT
from recpath.safety import error_summary
from recpath.storage import session_to_csv, sessions_to_csv, sessions_to_json


LIBRARY_SELECTION = "library_selected"
LIBRARY_NAV = "_library_nav"


def open_session(session_id: str) -> None:
    """Show ``session_id`` and redraw the table so its checked row follows (parent/child links)."""
    st.session_state[LIBRARY_SELECTION] = session_id
    st.session_state[LIBRARY_NAV] = st.session_state.get(LIBRARY_NAV, 0) + 1
config = get_config()


# ---------------------------------------------------------------------- starting follow-up runs
def chain_follow_up_kwargs(
    session: dict[str, Any],
    *,
    start_result: dict[str, Any],
    parent_round: int | None,
    strategy: str,
    rounds: int,
) -> dict[str, Any]:
    """The exact start_chain() arguments a continuation or branch of a browser session uses."""
    return run_manager.continuation_kwargs(
        session,
        start_result=start_result,
        parent_round=parent_round,
        strategy=strategy,
        max_rounds=rounds,
        options=browser_options(config),
        region=config.region,
        language=config.language,
        default_system_prompt=SYSTEM_PROMPT,
        default_openai_model=config.openai_model,
    )


def start_follow_up(
    session: dict[str, Any],
    *,
    start_result: dict[str, Any],
    parent_round: int | None,
    strategy: str,
    rounds: int,
    chain_kwargs: dict[str, Any] | None = None,
) -> str | None:
    """Continue or branch ``session`` through the run manager. Returns an error text or None."""
    mode = library.session_mode(session)
    try:
        if mode == MODE_CHAIN:
            kwargs = chain_kwargs or chain_follow_up_kwargs(
                session, start_result=start_result, parent_round=parent_round, strategy=strategy, rounds=rounds
            )
            run_manager.start_chain(**kwargs)
        elif mode == MODE_API:
            if not config.youtube_api_key:
                return "API metadata mode needs a YouTube API key (YOUTUBE_API_KEY in .streamlit/secrets.toml)."
            run_manager.start_api_chain(
                youtube_api_key=config.youtube_api_key,
                entry_type="video",
                entry_value=start_result.get("title") or "",
                entry_video=start_result,
                parent={
                    "session_id": session.get("session_id"),
                    "round": parent_round,
                    "video_id": start_result.get("video_id"),
                    "title": start_result.get("title"),
                },
                strategy=strategy,
                max_rounds=rounds,
                max_results=int(remembered("api_results", 50)),
                region=config.region,
                language=config.language,
                user_prompt=session.get("user_prompt") or "",
                generated_queries=session.get("generated_queries") or [],
                selected_query=session.get("selected_query"),
                system_prompt=session.get("system_prompt") or SYSTEM_PROMPT,
                openai_model=session.get("openai_model") or config.openai_model,
            )
        else:
            return "Only recommendation chains and API metadata sessions can be continued."
    except run_manager.RunAlreadyActive as exc:
        return error_summary(exc)
    except Exception as exc:
        return f"Could not start the run: {error_summary(exc)}"
    return None


def follow_up_controls(session: dict[str, Any], prefix: str) -> tuple[int, str]:
    rounds_col, strategy_col = st.columns([1, 1.3], vertical_alignment="bottom")
    with rounds_col:
        rounds = int(
            persistent(
                st.number_input, f"{prefix}_rounds", default_rounds(config), "Rounds", min_value=1, max_value=10, step=1
            )
        )
    parent_strategy = session.get("strategy") if session.get("strategy") in STRATEGY_LABELS else "top_1"
    with strategy_col:
        strategy = st.segmented_control(
            "Pick strategy",
            list(STRATEGY_LABELS),
            default=parent_strategy,
            format_func=STRATEGY_LABELS.get,
            required=True,
            key=f"{prefix}_strategy_{session.get('session_id')}",
        ) or parent_strategy
    return rounds, strategy


def plan_caption(session: dict[str, Any], chain_kwargs: dict[str, Any] | None) -> str:
    """The settings the follow-up run will actually use, and where each one comes from."""
    if library.session_mode(session) != MODE_CHAIN or not chain_kwargs:
        return "Searches keywords derived from the start video, like the original API chain."
    values = chain_kwargs["settings"].public_dict()
    inherited = run_manager.inherited_settings(session)
    shown = [
        ("region", f"location {values.get('region')}"),
        ("device", str(values.get("device"))),
        ("consent_choice", f"cookies {CONSENT_LABELS.get(values.get('consent_choice'), values.get('consent_choice'))}"),
        ("navigation", NAVIGATION_LABELS.get(values.get("navigation"), str(values.get("navigation")))),
        ("exclude_policy", f"exclude {EXCLUDE_POLICY_LABELS.get(values.get('exclude_policy'), values.get('exclude_policy'))}"),
        ("max_candidates", f"scan depth {values.get('max_candidates')}"),
    ]
    if isinstance(values.get("watch_dwell_ms"), (int, float)) and "watch_dwell_ms" in run_manager.settings_field_names():
        shown.append(("watch_dwell_ms", f"watch {values['watch_dwell_ms'] / 1000:.1f} s"))
    parts = [text + ("" if name in inherited else " (New run page)") for name, text in shown]
    excluded = len(chain_kwargs.get("initial_seen_ids") or [])
    if session.get("settings"):
        lead = "Runs with this session's measurement settings"
    else:
        lead = (
            "This older session did not record its browser settings; values marked (New run page) "
            "come from that page, the device from the session itself. Runs with"
        )
    return f"{lead}: {' · '.join(md(part) for part in parts)}. {excluded} video(s) it already excluded stay excluded."


# ---------------------------------------------------------------------- round dialog
@st.dialog("Round details", width="large")
def round_dialog(session_id: str, round_number: int) -> None:
    session = library.load_session(session_id) or {}
    record = next((item for item in library.rounds_of(session) if item.get("round") == round_number), None)
    if record is None:
        st.warning("This round could not be loaded.")
        return
    selected = library.selected_of(record)
    st.markdown(
        f"**Round {round_number}** · {source_label(record.get('source'))} · {md(record.get('fetched_at_local') or '')}"
    )
    selection = record.get("selection") if isinstance(record.get("selection"), dict) else {}
    if selected:
        st.caption(f"Picked: {md(selected.get('title'))} · {md(selection.get('reason') or '')}")
    if record.get("derived_query"):
        st.caption(f"Searched: {md(record.get('derived_query'))} ({md(record.get('query_source'))})")

    shots = library.round_screenshots(record)
    facts = round_facts(record)
    captions = {"landing": "On arrival", "selected": "Selected card", "ad": "Player ad"}
    if shots:
        shot_col, facts_col = st.columns([1.15, 2], gap="medium")
        with shot_col:
            with st.container(horizontal=True, gap="small"):
                for kind, path in shots.items():
                    data = thumb_bytes(path, 300)
                    if data:
                        with st.container(width=112, gap="small"):
                            st.image(data, width="stretch")
                            st.caption(captions.get(kind, kind))
    else:
        facts_col = st.container()
    with facts_col:
        st.markdown("**Round facts**")
        st.dataframe(
            pd.DataFrame(facts, columns=["fact", "value"]),
            hide_index=True,
            width="stretch",
            height=38 + 35 * max(len(facts), 1),
            column_config={
                "fact": st.column_config.TextColumn("Fact", width="medium"),
                "value": st.column_config.TextColumn("Value", width="large"),
            },
        )

    frame = candidates_frame(session, [round_number])
    with st.expander("Round record (JSON, without the item lists)", icon=":material/data_object:"):
        st.json(round_record_without_items(record), expanded=1)
    if frame.empty:
        st.info("No items were recorded in this round.")
        return
    st.markdown("**Cards on the page**")
    event = st.dataframe(
        frame,
        column_order=candidate_columns(frame, with_round=False, compact=True),
        column_config=CANDIDATE_COLUMN_CONFIG,
        hide_index=True,
        width="stretch",
        height=420,
        row_height=48,
        placeholder="",
        on_select="rerun",
        selection_mode="single-row",
        key=f"dialog_table_{session_id}_{round_number}",
    )
    mode = library.session_mode(session)
    if mode not in (MODE_CHAIN, MODE_API):
        return
    rows = event.selection.rows if event and event.selection else []
    choice = frame.iloc[rows[0]] if rows else None
    st.markdown("**Branch from a displayed video**")
    if choice is None:
        st.caption("Select a normal video in the table to start a new chain from it.")
        return
    item = next(
        (entry for entry in library.round_items(record) if entry.get("video_id") == choice["video_id"]),
        None,
    )
    if not item or item.get("label") != "normal_video" or not item.get("video_id"):
        st.caption("Only normal videos can start a branch (Shorts, ads, playlists and mixes cannot).")
        return
    st.caption(f"Start video: {md(item.get('title') or item.get('video_id'))}.")
    rounds, strategy = follow_up_controls(session, "branch")
    chain_kwargs = (
        chain_follow_up_kwargs(session, start_result=item, parent_round=round_number, strategy=strategy, rounds=rounds)
        if mode == MODE_CHAIN else None
    )
    st.caption(plan_caption(session, chain_kwargs))
    active = run_manager.current()
    if active is not None:
        st.caption("A run is in progress; wait for it to finish before branching.")
    if st.button(
        f"Branch from this video ({rounds} rounds)",
        type="primary",
        icon=":material/call_split:",
        disabled=active is not None,
    ):
        error = start_follow_up(session, start_result=item, parent_round=round_number, strategy=strategy, rounds=rounds,
                                chain_kwargs=chain_kwargs)
        if error:
            st.error(md(error))
        else:
            st.switch_page(PAGE_LIVE)


# ---------------------------------------------------------------------- detail tabs
def render_timeline(session: dict[str, Any]) -> None:
    rounds = library.rounds_of(session)
    if not rounds:
        st.info("This session has no finished rounds.")
        return
    shots_by_round = [library.round_screenshots(record) for record in rounds]
    captions = {"landing": "On arrival", "selected": "Selected card", "ad": "Player ad"}
    # One image column per screenshot kind this session has; none at all (API mode, missing
    # files) falls back to the picked video's YouTube thumbnail.
    slots = [kind for kind in captions if any(kind in shots for shots in shots_by_round)]
    for record, shots in zip(rounds, shots_by_round):
        number = record.get("round")
        selected = library.selected_of(record)
        with st.container(border=True):
            columns = st.columns([1] * max(len(slots), 1) + [2.4 if len(slots) > 1 else 3.2], gap="medium")
            if slots:
                for column, kind in zip(columns, slots):
                    with column:
                        data = thumb_bytes(shots.get(kind), 360) if shots.get(kind) else None
                        if data:
                            st.image(data, caption=captions[kind], width=170)
                        elif kind == "ad":
                            if library.player_observed(record):
                                st.caption("No player ad")
                        else:
                            st.caption(f"{captions[kind]}: no screenshot")
            else:
                with columns[0]:
                    thumb = library.thumbnail_url(selected.get("video_id"))
                    if thumb:
                        st.image(thumb, width=240, caption="Picked video")
            with columns[-1]:
                with st.container(horizontal=True, gap="small", vertical_alignment="center"):
                    st.badge(f"Round {number} · {source_label(record.get('source'))}", color="primary")
                    if record.get("metadata_enriched"):
                        st.badge("API-enriched", color="green")
                    if record.get("fetched_at_local"):
                        st.caption(str(record.get("fetched_at_local")))
                if selected:
                    url = selected.get("url") or library.watch_url(selected.get("video_id"))
                    st.markdown(f"**[{md(shorten(selected.get('title') or selected.get('video_id'), 110))}]({url})**")
                    bits = []
                    if selected.get("channel_name"):
                        bits.append(md(selected["channel_name"]))
                    position = library.selected_position(record)
                    if position:
                        count = record.get("raw_result_count")
                        bits.append(f"position {position} of {count}" if count else f"position {position}")
                    navigation = record.get("navigation") or {}
                    if navigation.get("method"):
                        bits.append(NAVIGATION_DONE.get(navigation["method"], f"opened by {navigation['method']}"))
                    if navigation.get("error"):
                        bits.append(f"navigation error: {md(shorten(navigation['error'], 80))}")
                    st.caption(" · ".join(bits))
                else:
                    st.markdown("**No video selected**")
                    st.caption(md((record.get("selection") or {}).get("reason") or "No eligible normal video was left."))
                if record.get("derived_query"):
                    st.caption(f"Searched: {md(record.get('derived_query'))}")
                label_chips(library.label_counts(record))
                duplicates = record.get("filtered_duplicate_count")
                if duplicates:
                    st.caption(f"{duplicates} item(s) excluded as already seen.")
                if record.get("metadata_enrichment_error"):
                    st.caption(f"API enrichment failed: {md(shorten(record['metadata_enrichment_error'], 120))}")
                if st.button("Details and branching", key=f"details_{session.get('session_id')}_{number}",
                             icon=":material/table_rows:"):
                    round_dialog(str(session.get("session_id")), number)


def render_graph(session: dict[str, Any]) -> None:
    session_id = str(session.get("session_id"))
    children_summaries = library.children_of(session_id)
    children = [library.load_session(item["session_id"]) for item in children_summaries]
    children = [child for child in children if child]
    grandchildren: dict[str, list[dict[str, Any]]] = {}
    for child in children:
        nested = [library.load_session(item["session_id"]) for item in library.children_of(str(child.get("session_id")))]
        grandchildren[str(child.get("session_id"))] = [item for item in nested if item]
    if not library.rounds_of(session):
        st.info("This session has no rounds to draw.")
        return
    dot = chain_dot(session, children, grandchildren=grandchildren)
    # A short chain at natural size stays readable; a long one is scaled to the page width.
    columns = max(len(library.rounds_of(item)) for item in [session, *children]) + 2
    st.graphviz_chart(dot, width="content" if columns <= 6 else "stretch")
    st.caption("Each box is the video picked in that round; an edge label such as #2/20 is the pick's position among "
               "the cards on that page. Dashed blue boxes are continuations and branches saved as their own sessions.")
    if children_summaries:
        st.markdown("**Continuations and branches**")
        for item in children_summaries:
            with st.container(horizontal=True, gap="small", vertical_alignment="center"):
                st.caption(
                    f"From round {item.get('parent_round')} · {short_created(item.get('created_local'))} · "
                    f"{item.get('rounds')} round(s) · {shown_status(item)}"
                )
                if st.button("Open", key=f"open_child_{item['session_id']}", type="tertiary", icon=":material/arrow_forward:"):
                    open_session(item["session_id"])
                    st.rerun()


def render_candidates(session: dict[str, Any]) -> None:
    frame = candidates_frame(session)
    if frame.empty:
        st.info("No candidates were recorded.")
        return
    session_id = session.get("session_id")
    labels = [label for label in dict.fromkeys(frame["label"]) if label]
    filter_col, round_col = st.columns([3, 2], vertical_alignment="bottom")
    with filter_col:
        chosen = st.pills(
            "Card types",
            labels,
            selection_mode="multi",
            default=labels,
            format_func=lambda label: f"{label_name(label)} ({int((frame['label'] == label).sum())})",
            key=f"cand_labels_{session_id}",
        )
    with round_col:
        round_numbers = sorted({int(value) for value in frame["round"].dropna()})
        picked_round = st.segmented_control(
            "Round",
            ["All", *round_numbers],
            default="All",
            format_func=lambda value: value if value == "All" else f"R{value}",
            required=True,
            key=f"cand_round_{session_id}",
        )
    view = frame[frame["label"].isin(chosen or [])]
    if picked_round not in (None, "All"):
        view = view[view["round"] == picked_round]
    st.caption(f"{len(view)} of {len(frame)} observed items.")
    st.dataframe(
        view,
        column_order=candidate_columns(view if not view.empty else frame),
        column_config=CANDIDATE_COLUMN_CONFIG,
        hide_index=True,
        width="stretch",
        height=520,
        row_height=48,
        placeholder="",
    )


def render_shorts(session: dict[str, Any]) -> None:
    items = library.shorts_items(session)
    if not items:
        st.info("No Shorts feed items were recorded.")
        return
    shorts_filmstrip(items, card_width=150)
    st.dataframe(shorts_frame(items), column_config=SHORTS_COLUMN_CONFIG, hide_index=True, width="stretch",
                 row_height=48, placeholder="")


def shown_status(summary: dict[str, Any]) -> str:
    """A summary's status as this page shows it: "running" only while this app is writing it."""
    active = run_manager.current()
    return library.display_status(summary.get("status"), summary.get("session_id"), active.session_id if active else None)


def status_message(session: dict[str, Any], status: str) -> None:
    error = session.get("error") if isinstance(session.get("error"), dict) else {}
    rounds = library.rounds_of(session)
    mode = library.session_mode(session)
    if status == library.UNFINISHED_STATUS:
        kept = (
            f" {len(rounds)} round(s) were saved." if mode != MODE_SHORTS
            else f" {len(library.shorts_items(session))} Short(s) were saved."
        )
        st.warning(UNFINISHED_NOTE + kept, icon=":material/hourglass_disabled:")
    elif status == "error":
        shorts_mode = mode == MODE_SHORTS
        finished = len(library.shorts_items(session)) if shorts_mode else len(rounds)
        unit = "Short" if shorts_mode else "round"
        where = error_place(error.get("round"), finished, unit) or f"before the first {unit}"
        kept = f" The {'Shorts' if shorts_mode else 'rounds'} finished before the error were kept." if finished else ""
        st.warning(f"The run stopped {where}: {sentence(error.get('message'))}.{kept}", icon=":material/error:")
    elif status == "interrupted":
        unit = "Shorts" if mode == MODE_SHORTS else "rounds"
        st.warning(f"The run was interrupted; the {unit} finished before that were kept.", icon=":material/warning:")
    elif session.get("end_reason") == "feed_stuck":
        st.info(
            f"The Shorts feed stopped advancing after {len(library.shorts_items(session))} item(s).",
            icon=":material/info:",
        )
    elif session.get("end_reason") == "no_candidates":
        st.info("The chain ended early: no eligible normal video was left to select.", icon=":material/info:")
    elif session.get("end_reason") == "feed_not_found":
        st.warning(
            "The chain ended early: no recommendation list was found on the page (YouTube's layout may have changed).",
            icon=":material/warning:",
        )
    if library.consent_hit_wall(session):
        st.warning("At least one round landed on YouTube's cookie-consent page instead of the feed.", icon=":material/cookie:")
    save_errors = session.get("save_errors") if isinstance(session.get("save_errors"), list) else []
    if save_errors and session.get("last_save_ok") is False:
        last = save_errors[-1] if isinstance(save_errors[-1], dict) else {}
        st.warning(f"Saving to disk failed ({sentence(last.get('message'))}).")


def render_detail(session: dict[str, Any], summary: dict[str, Any]) -> None:
    session_id = str(session.get("session_id"))
    mode = library.session_mode(session)
    rounds = library.rounds_of(session)
    st.divider()
    head, numbers = st.columns([3, 2.2], gap="large", vertical_alignment="top")
    with head:
        st.subheader(md(summary.get("query") or "(no query)"))
        with st.container(horizontal=True, gap="small"):
            st.badge(MODE_LABELS.get(mode, mode), color="blue", icon=":material/smartphone:" if mode != MODE_API else ":material/api:")
            if not library.is_legacy(session):
                st.badge(summary["status"], color=STATUS_COLORS.get(summary["status"], "gray"),
                         help=UNFINISHED_NOTE if summary["status"] == library.UNFINISHED_STATUS else None)
            if summary.get("strategy") and mode != MODE_SHORTS:
                st.badge(f"strategy {strategy_label(summary['strategy'])}", color="gray")
            if summary.get("device"):
                st.badge(str(summary["device"]), color="gray")
            if summary.get("location") and mode != MODE_API:
                st.badge(f"location {summary['location']}", color="violet", icon=":material/public:")
            if mode != MODE_API:
                st.badge(f"cookies: {summary['consent']}", color="orange" if summary["consent_wall"] else "gray")
                st.badge("API-enriched" if summary.get("api_enriched") else "page data only",
                         color="green" if summary.get("api_enriched") else "gray")
            if summary.get("is_branch"):
                relation = {"continuation": "continued from", "branch": "branch from"}.get(summary.get("parent_kind"), "from")
                st.badge(f"{relation} R{summary.get('parent_round')}", color="violet", icon=":material/call_split:")
            if library.is_legacy(session):
                st.badge("older format", color="gray", help="Saved before statuses, settings and consent were recorded.")
        reason = end_reason_text(session.get("end_reason"), mode) if summary["status"] != library.UNFINISHED_STATUS else ""
        st.caption(" · ".join(bit for bit in (session_id, f"created {summary.get('created_local')}", reason) if bit))
    with numbers:
        planned = summary.get("max_rounds")
        tiles = [("Rounds", f"{len(rounds)} / {planned}" if planned else len(rounds), None)]
        if mode == MODE_SHORTS:
            tiles = [("Shorts items", summary.get("items"), None)]
        tiles += [
            ("Shorts seen", summary.get("shorts"), "Shorts cards in the feeds (or Shorts watched in Shorts feed mode)"),
            ads_tile(summary.get("ads"), summary.get("ads_scope")),
        ]
        if mode != MODE_SHORTS:
            tiles.append(("Channels", library.distinct_channels(session), "Distinct channels among the observed normal videos"))
        metric_row(tiles)

    status_message(session, summary["status"])

    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        if summary.get("is_branch") and summary.get("parent_session_id"):
            if st.button("Open parent session", icon=":material/arrow_upward:", key=f"parent_{session_id}"):
                open_session(summary["parent_session_id"])
                st.rerun()
        last = rounds[-1] if rounds else {}
        last_pick = library.selected_of(last)
        if mode in (MODE_CHAIN, MODE_API) and last_pick:
            with st.popover("Continue from the last pick", icon=":material/redo:"):
                st.caption(f"Starts from “{md(shorten(last_pick.get('title') or last_pick.get('video_id'), 80))}”.")
                rounds_count, strategy = follow_up_controls(session, "continue")
                chain_kwargs = (
                    chain_follow_up_kwargs(session, start_result=last_pick, parent_round=last.get("round"),
                                           strategy=strategy, rounds=rounds_count)
                    if mode == MODE_CHAIN else None
                )
                st.caption(plan_caption(session, chain_kwargs))
                active = run_manager.current()
                if active is not None:
                    st.caption("A run is in progress; wait for it to finish first.")
                if st.button(f"Continue {rounds_count} more rounds", type="primary", icon=":material/play_arrow:",
                             disabled=active is not None, key=f"continue_{session_id}"):
                    error = start_follow_up(session, start_result=last_pick, parent_round=last.get("round"),
                                            strategy=strategy, rounds=rounds_count, chain_kwargs=chain_kwargs)
                    if error:
                        st.error(md(error))
                    else:
                        st.switch_page(PAGE_LIVE)
        st.download_button(
            "JSON", data=lambda: sessions_to_json([session]), file_name=f"{session_id}.json",
            mime="application/json", icon=":material/download:", on_click="ignore", key=f"dl_json_{session_id}",
        )
        st.download_button(
            "CSV", data=lambda: session_to_csv(library.export_safe(session)), file_name=f"{session_id}.csv",
            mime="text/csv", icon=":material/download:", on_click="ignore", key=f"dl_csv_{session_id}",
        )

    # A Shorts feed has no rounds, so the round-based tabs would only say so.
    names = ["Shorts feed", "Ads", "Raw JSON"] if mode == MODE_SHORTS else ["Timeline", "Path graph", "Candidates", "Ads", "Raw JSON"]
    tabs = dict(zip(names, st.tabs(names)))
    if "Shorts feed" in tabs:
        with tabs["Shorts feed"]:
            render_shorts(session)
    if "Timeline" in tabs:
        with tabs["Timeline"]:
            render_timeline(session)
        with tabs["Path graph"]:
            render_graph(session)
        with tabs["Candidates"]:
            render_candidates(session)
    with tabs["Ads"]:
        ads_panel(session)
    with tabs["Raw JSON"]:
        st.caption("The saved session file as it is on disk (the canonical record).")
        st.json(session, expanded=1)


# ---------------------------------------------------------------------- page
st.title("Session library")
st.caption(f"Every saved run in `{paths.project_relative(paths.SESSIONS_DIR)}`. Select a row to open it.")
_active = run_manager.current()
_active_session_id = _active.session_id if _active else None
# Copies: the cached summaries must keep the status that is on disk.
summaries = [
    {**item, "status": library.display_status(item["status"], item["session_id"], _active_session_id)}
    for item in library.index()
]
if not summaries:
    st.info(f"No saved sessions yet in `{paths.SESSIONS_DIR}`.", icon=":material/inbox:")
    st.page_link(PAGE_NEW, label="Start a new run", icon=":material/add_circle:")
    st.stop()

deep_link = st.query_params.get("session")
if deep_link and st.session_state.get("_library_deep_link") != deep_link:
    st.session_state["_library_deep_link"] = deep_link
    st.session_state[LIBRARY_SELECTION] = deep_link

present_modes = sorted({item["mode"] for item in summaries}, key=lambda mode: (list(MODE_LABELS).index(mode)
                                                                       if mode in MODE_LABELS else len(MODE_LABELS), mode))
present_statuses = list(dict.fromkeys(item["status"] for item in summaries))
f_mode, f_status, f_text, f_branch = st.columns([2.2, 1.1, 2, 1], vertical_alignment="bottom")
with f_mode:
    modes = persistent(
        st.pills, "lib_modes", [], "Mode", present_modes,
        selection_mode="multi", format_func=lambda value: MODE_LABELS.get(value, value),
        help="Show only these run types (none selected shows all).",
    )
    modes = [mode for mode in modes or [] if mode in present_modes]
with f_status:
    status_filter = persistent(st.selectbox, "lib_status", "All", "Status", ["All", *present_statuses])
with f_text:
    text = persistent(st.text_input, "lib_search", "", "Search", placeholder="Query, picked title or session id")
with f_branch:
    hide_branches = persistent(st.toggle, "lib_hide_branches", False, "Hide branches")

needle = (text or "").strip().casefold()
view = [
    item for item in summaries
    if item["mode"] in (modes or present_modes)
    and (status_filter in (None, "All") or item["status"] == status_filter)
    and not (hide_branches and item["is_branch"])
    and (
        not needle
        or needle in item["query"].casefold()
        or needle in (item["last_selected_title"] or "").casefold()
        or needle in item["session_id"].casefold()
    )
]

table = pd.DataFrame(
    [
        {
            "thumb": item["first_thumbnail"],
            "created": short_created(item["created_local"]),
            "query": item["query"],
            "mode": MODE_SHORT_LABELS.get(item["mode"], item["mode"]),
            "device": item["device"] or "",
            "location": item.get("location") or "",
            "consent": item["consent"],
            "rounds": (f"{item['rounds']} / {item['max_rounds']}" if item["max_rounds"] else str(item["rounds"]))
            if item["mode"] != MODE_SHORTS else f"{item['items']} Shorts",
            "progress": min(item["rounds"] / item["max_rounds"], 1.0)
            if item["max_rounds"] and item["mode"] != MODE_SHORTS else None,
            "shorts": item["shorts"],
            # Blank for a count of feed ad cards alone: the player was not observed (unknown, not 0).
            "ads": item["ads"] if item.get("ads_scope") != library.ADS_SCOPE_FEED_ONLY else None,
            "status": item["status"],
            "branch": f"↳ from R{item['parent_round']}" if item["is_branch"] else "",
            "last_pick": item["last_selected_title"],
            "session_id": item["session_id"],
        }
        for item in view
    ],
    columns=["thumb", "created", "query", "mode", "device", "location", "consent", "rounds", "progress", "shorts", "ads",
             "status", "branch", "last_pick", "session_id"],
)
view_ids = [item["session_id"] for item in view]
selected_id = st.session_state.get(LIBRARY_SELECTION)
table_key = "lib_table_" + str(abs(hash((
    tuple(modes or []), status_filter, needle, hide_branches, st.session_state.get(LIBRARY_NAV, 0)
))))
default_rows = [view_ids.index(selected_id)] if selected_id in view_ids else []
fresh_table = table_key not in st.session_state  # first draw after a page switch or filter change
event = st.dataframe(
    table,
    key=table_key,
    on_select="rerun",
    selection_mode="single-row",
    selection_default={"selection": {"rows": default_rows}} if default_rows else None,
    hide_index=True,
    width="stretch",
    height=min(38 + 35 * max(len(table), 1), 360),
    placeholder="",
    column_order=["thumb", "created", "query", "mode", "device", "location", "consent", "rounds", "progress", "shorts", "ads",
                  "status", "branch", "last_pick"],
    column_config={
        "thumb": st.column_config.ImageColumn("", width=64, help="First picked video"),
        "created": st.column_config.TextColumn("Created", width=118),
        "query": st.column_config.TextColumn("Start query", width=210),
        "mode": st.column_config.TextColumn("Mode", width=82),
        "device": st.column_config.TextColumn("Device", width=82),
        "location": st.column_config.TextColumn("Location", width=70, help="YouTube's content location (gl) for the run"),
        "consent": st.column_config.TextColumn("Cookies", width=104),
        "rounds": st.column_config.TextColumn("Rounds", width=70),
        "progress": st.column_config.ProgressColumn("Done", width=88, min_value=0.0, max_value=1.0, format="percent"),
        "shorts": st.column_config.NumberColumn("Shorts", width=62),
        "ads": st.column_config.NumberColumn(
            "Ads", width=50,
            help="Feed ad cards plus player ads (or ads in a Shorts feed). Blank when the session only labelled "
            "ad cards in the feeds and never observed the video player, so player ads are unknown.",
        ),
        "status": st.column_config.TextColumn("Status", width=92),
        "branch": st.column_config.TextColumn("Branch", width=92),
        "last_pick": st.column_config.TextColumn("Last pick", width="large"),
    },
)
rows = [row for row in (event.selection.rows if event and event.selection else []) if row < len(view_ids)]
last_key, last_rows = st.session_state.get("_library_last_rows", (None, None))
if rows and (fresh_table or rows != last_rows or last_key != table_key):
    st.session_state[LIBRARY_SELECTION] = view_ids[rows[0]]
elif not rows and not fresh_table and last_key == table_key and last_rows:
    st.session_state[LIBRARY_SELECTION] = None  # the user cleared the selection
st.session_state["_library_last_rows"] = (table_key, rows)

with st.container(horizontal=True, gap="small", vertical_alignment="center"):
    st.caption(f"{len(view)} of {len(summaries)} sessions")
    filtered_ids = list(view_ids)

    def filtered_sessions() -> list[dict[str, Any]]:
        return [session for session in (library.load_session(item) for item in filtered_ids) if session]

    def filtered_csv() -> str:
        # One odd file (a non-dict round, None where a list belongs) must not fail the whole export:
        # every session is cleaned first, and one that still cannot be flattened is left out.
        cleaned = [library.export_safe(session) for session in filtered_sessions()]
        try:
            return sessions_to_csv(cleaned)
        except Exception:
            kept = []
            for session in cleaned:
                try:
                    session_to_csv(session)
                except Exception:
                    continue
                kept.append(session)
            return sessions_to_csv(kept)

    st.download_button(
        f"All {len(view)} as JSON", data=lambda: sessions_to_json(filtered_sessions()),
        file_name="recpath_sessions.json", mime="application/json",
        icon=":material/download:", on_click="ignore", type="tertiary", disabled=not view,
    )
    st.download_button(
        f"All {len(view)} as CSV", data=filtered_csv,
        file_name="recpath_sessions.csv", mime="text/csv",
        icon=":material/download:", on_click="ignore", type="tertiary", disabled=not view,
    )

selected_id = st.session_state.get(LIBRARY_SELECTION)
if not selected_id:
    st.caption("Select a session in the table to open it.")
    st.stop()
session = library.load_session(selected_id)
summary = next((item for item in summaries if item["session_id"] == selected_id), None)
if session is None or summary is None:
    st.warning("The selected session could not be read from disk.")
    st.stop()
render_detail(session, summary)
