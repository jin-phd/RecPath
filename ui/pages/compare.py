"""Compare: how similar are the paths and recommendation lists of 2-4 sessions?"""
from __future__ import annotations

from itertools import combinations
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from ui.common import (
    MODE_SHORT_LABELS,
    PAGE_NEW,
    md,
    remember,
    remembered,
    shorten,
    strategy_label,
    widget_key,
)
from ui.components import metric_row
from recpath import library
from recpath.library import MODE_LABELS, MODE_SHORTS


LETTERS = "ABCD"
ORGANIC = ("normal_video", "short", "live", "movie", "upcoming")


def jaccard(left: set[str], right: set[str]) -> float | None:
    """Overlap of two id sets; None (unknown, not 0%) when either side recorded nothing,
    e.g. a round that landed on the cookie page."""
    if not left or not right:
        return None
    return len(left & right) / len(left | right)


def mean(values: list[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return sum(known) / len(known) if known else None


def percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


# Validated categorical order (see .streamlit/config.toml); a pair keeps its colour.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]


def overlap_chart(frame: pd.DataFrame) -> alt.Chart:
    """Grouped bars: per round, the Jaccard overlap of each pair's recommendation lists."""
    long = frame.reset_index().melt(id_vars="round", var_name="pair", value_name="overlap").dropna()
    pairs = list(frame.columns)
    return (
        alt.Chart(long)
        .mark_bar(size=22, cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
        .encode(
            x=alt.X("round:N", title="Round", sort=list(frame.index), axis=alt.Axis(labelAngle=0),
                    scale=alt.Scale(domain=list(frame.index))),  # keep rows without a value on the axis
            xOffset=alt.XOffset("pair:N", sort=pairs),
            y=alt.Y("overlap:Q", title="Jaccard overlap of normal videos", scale=alt.Scale(domain=[0, 1]),
                    axis=alt.Axis(format="%", tickCount=5)),
            color=alt.Color("pair:N", title="Pair", sort=pairs,
                            scale=alt.Scale(domain=pairs, range=SERIES_COLORS[: len(pairs)]),
                            legend=alt.Legend(orient="top") if len(pairs) > 1 else None),
            tooltip=[alt.Tooltip("round:N", title="Round"), alt.Tooltip("pair:N", title="Pair"),
                     alt.Tooltip("overlap:Q", title="Overlap", format=".0%")],
        )
        .properties(height=260)
    )


def default_selection(summaries: list[dict[str, Any]]) -> list[str]:
    """The two newest independent sessions that share a start query (else the two newest).

    Branches and continuations are skipped: their first round is not a search, so they are no
    repeat of their parent's run.
    """
    roots = [item for item in summaries if not item.get("is_branch")]
    first_by_query: dict[str, str] = {}
    for item in roots:
        query = item["query"].casefold()
        if not query:
            continue
        if query in first_by_query:
            return [first_by_query[query], item["session_id"]]
        first_by_query[query] = item["session_id"]
    return [item["session_id"] for item in (roots if len(roots) >= 2 else summaries)[:2]]


def option_label(summary: dict[str, Any]) -> str:
    """A unique label per session: Streamlit maps a multiselect's labels back to options, so two
    sessions with the same label (same query and minute, as repeated runs often are) would merge."""
    created = str(summary.get("created_local") or "")[:19]
    query = shorten(summary.get("query") or "(no query)", 48)
    device = summary.get("device") or MODE_LABELS.get(summary.get("mode"), summary.get("mode"))
    location = summary.get("location") or "?"
    return f"{created} · {query} · {location} · {device} · {summary.get('consent')} · …{str(summary.get('session_id'))[-6:]}"


def depth_offset(summary: dict[str, Any], index_by_id: dict[str, dict[str, Any]]) -> int:
    """Rounds between the start search and this session's first round.

    A branch or continuation from round r of its parent starts one level deeper than that
    round: its round 1 is the Up next list reached from round r's page, like the parent's
    round r + 1. Nested branches add up their parents' offsets.
    """
    offset = 0
    seen: set[str] = set()
    current: dict[str, Any] | None = summary
    while current and current.get("is_branch") and isinstance(current.get("parent_round"), int):
        if current["session_id"] in seen:
            break
        seen.add(current["session_id"])
        offset += max(current["parent_round"], 0)
        current = index_by_id.get(str(current.get("parent_session_id")))
    return offset


st.title("Compare sessions")
st.caption("Repeated runs of the same query, or the same query under different content locations, devices or cookie answers: how far do the paths drift apart?")
# Streamlit cuts chip labels at 128 px ("2026-10-22 09:56…"), which hides what tells two runs of
# one query apart; let the chips show the whole label.
st.html(
    "<style>[data-testid='stMultiSelect'] [data-baseweb='tag'] > span:first-child"
    "{max-width:min(36rem, calc(100vw - 7rem))}</style>"
)

all_summaries = library.index()
index_by_id = {item["session_id"]: item for item in all_summaries}
summaries = [item for item in all_summaries if item["rounds"] > 0 and item["mode"] != MODE_SHORTS]
if len(summaries) < 2:
    st.info("At least two saved sessions with rounds are needed to compare.", icon=":material/compare_arrows:")
    st.page_link(PAGE_NEW, label="Start a new run", icon=":material/add_circle:")
    st.stop()

by_id = {item["session_id"]: item for item in summaries}
options = list(by_id)
stored = list(dict.fromkeys(value for value in (remembered("compare_ids") or []) if value in by_id))[:4]
if not stored:
    stored = default_selection(summaries)
key = widget_key("compare_ids")
if key not in st.session_state or any(value not in by_id for value in st.session_state[key]):
    st.session_state[key] = stored
chosen = st.multiselect(
    "Sessions to compare (2–4)",
    options,
    key=key,
    max_selections=4,
    format_func=lambda value: option_label(by_id[value]),
    help="Defaults to the two newest sessions with the same start query (branches are not picked by default). "
    "Each label ends with the last digits of the session id.",
)
chosen = list(dict.fromkeys(chosen))  # one letter per session, even if a value arrives twice
remember("compare_ids", chosen)
if len(chosen) < 2:
    st.info("Pick at least two sessions.")
    st.stop()

sessions = [library.load_session(value) for value in chosen]
if any(session is None for session in sessions):
    st.warning("A selected session could not be read from disk.")
    st.stop()
letters = {value: LETTERS[index] for index, value in enumerate(chosen)}
offsets = {value: depth_offset(by_id[value], index_by_id) for value in chosen}
aligned = any(offsets.values())

# ---------------------------------------------------------------------- overview
overview = []
for value, session in zip(chosen, sessions):
    summary = by_id[value]
    overview.append(
        {
            "session": letters[value],
            "created": str(summary["created_local"] or "")[:19],  # seconds: repeated runs share a minute
            "query": summary["query"],
            "mode": MODE_SHORT_LABELS.get(summary["mode"], summary["mode"]),
            "device": summary["device"] or "",
            "location": summary.get("location") or "",
            "consent": summary["consent"],
            "strategy": strategy_label(summary["strategy"]),
            "rounds": summary["rounds"],
            "channels_picked": library.distinct_channels(session, picks_only=True),
            "channels_seen": library.distinct_channels(session),
            "shorts": summary["shorts"],
            # A count of feed ad cards alone is no "ads seen" figure: the player was not observed.
            "ads": summary["ads"] if summary.get("ads_scope") != library.ADS_SCOPE_FEED_ONLY else None,
            "starts_at": f"R{offsets[value] + 1}" + (f" (from R{summary['parent_round']})" if summary.get("is_branch") else ""),
        }
    )
st.dataframe(
    pd.DataFrame(overview),
    column_order=[column for column in overview[0] if column != "starts_at" or aligned],
    hide_index=True,
    width="stretch",
    placeholder="",
    column_config={
        "session": st.column_config.TextColumn("", width=40),
        "created": st.column_config.TextColumn("Created", width=150),
        "query": st.column_config.TextColumn("Start query", width="medium"),
        "mode": st.column_config.TextColumn("Mode", width=80),
        "device": st.column_config.TextColumn("Device", width="small"),
        "location": st.column_config.TextColumn("Location", width="small"),
        "consent": st.column_config.TextColumn("Cookies", width="small"),
        "strategy": st.column_config.TextColumn("Strategy", width="small"),
        "rounds": st.column_config.NumberColumn("Rounds", width="small"),
        "channels_picked": st.column_config.NumberColumn("Channels (picks)", width=118,
                                                         help="Distinct channels among the picked videos"),
        "channels_seen": st.column_config.NumberColumn("Channels (all)", width=108,
                                                       help="Distinct channels among every observed normal video"),
        "shorts": st.column_config.NumberColumn("Shorts", width="small"),
        "ads": st.column_config.NumberColumn(
            "Ads", width="small",
            help="Feed ad cards plus player ads. Blank when a session only labelled ad cards in the feeds "
            "and never observed the video player.",
        ),
        "starts_at": st.column_config.TextColumn(
            "First row", width="small",
            help="Rows below are aligned by depth from the start search; a branch or continuation from "
            "round r starts at row r + 1.",
        ),
    },
)

# ---------------------------------------------------------------------- overlap
picked = {value: set(library.selected_ids(session)) for value, session in zip(chosen, sessions)}
per_round = {value: library.recommended_ids(session) for value, session in zip(chosen, sessions)}
observed = {
    value: set().union(*library.recommended_ids(session, labels=ORGANIC)) if library.rounds_of(session) else set()
    for value, session in zip(chosen, sessions)
}
pairs = list(combinations(chosen, 2))

st.subheader("Overlap")
if len(chosen) == 2:
    left, right = chosen
    common_picks = picked[left] & picked[right]
    common_observed = observed[left] & observed[right]
    metric_row(
        [
            ("Picked videos (Jaccard)", percent(jaccard(picked[left], picked[right])),
             f"{len(common_picks)} of {len(picked[left] | picked[right])} distinct picked videos are shared"),
            ("Recommended videos (Jaccard)", percent(jaccard(observed[left], observed[right])),
             f"{len(common_observed)} of {len(observed[left] | observed[right])} distinct observed videos and Shorts are shared"),
            ("Shared picks", len(common_picks), None),
            ("Shared recommendations", len(common_observed), None),
        ]
    )
else:
    all_picks = set.intersection(*picked.values())
    all_observed = set.intersection(*observed.values())
    metric_row(
        [
            ("Picked by all", len(all_picks), "Videos every selected session picked"),
            ("Recommended to all", len(all_observed), "Videos and Shorts every selected session observed"),
            ("Mean picked Jaccard", percent(mean([jaccard(picked[a], picked[b]) for a, b in pairs])), None),
            ("Mean recommended Jaccard", percent(mean([jaccard(observed[a], observed[b]) for a, b in pairs])), None),
        ]
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "pair": f"{letters[a]}–{letters[b]}",
                    "picked": jaccard(picked[a], picked[b]),
                    "recommended": jaccard(observed[a], observed[b]),
                }
                for a, b in pairs
            ]
        ),
        hide_index=True,
        width="content",
        column_config={
            "pair": st.column_config.TextColumn("Pair"),
            "picked": st.column_config.ProgressColumn("Picked videos (Jaccard)", format="percent", min_value=0.0, max_value=1.0),
            "recommended": st.column_config.ProgressColumn("Recommended (Jaccard)", format="percent", min_value=0.0, max_value=1.0),
        },
    )

def at_depth(value: str, depth: int, items: list[Any]) -> Any:
    """The entry of a session's per-round list at aligned row ``depth`` (1-based), else None."""
    index = depth - offsets[value] - 1
    return items[index] if 0 <= index < len(items) else None


first_row = min(offsets.values()) + 1
last_row = max(offsets[value] + len(per_round[value]) for value in chosen)
rows_range = range(first_row, last_row + 1)
round_rows = []
for depth in rows_range:
    row: dict[str, Any] = {"round": f"R{depth}"}
    for a, b in pairs:
        left_ids, right_ids = at_depth(a, depth, per_round[a]), at_depth(b, depth, per_round[b])
        row[f"{letters[a]}–{letters[b]}"] = (
            jaccard(left_ids, right_ids) if left_ids is not None and right_ids is not None else None
        )
    round_rows.append(row)
round_frame = pd.DataFrame(round_rows).set_index("round")
chart_col, table_col = st.columns([2.2, 1], gap="large")
with chart_col:
    st.markdown("**Overlap of the recommendation lists, round by round**")
    if round_frame.notna().to_numpy().any():
        st.altair_chart(overlap_chart(round_frame), width="stretch")
    else:
        st.info(
            "No round has recorded lists in two of these sessions at the same depth (for example, "
            "one session continues the other, or a round recorded nothing), so there is nothing "
            "to compare round by round.",
            icon=":material/info:",
        )
    if aligned:
        shifted = ", ".join(
            f"{letters[value]} starts at R{offsets[value] + 1}" for value in chosen if offsets[value]
        )
        st.caption(
            "Rows are aligned by depth from the start search: a branch or continuation from round r starts "
            f"at row r + 1, next to what its parent reached from the same page ({shifted}). "
            "R1 is the search results; later rows are Up next lists."
        )
    else:
        st.caption("Round 1 compares the search results; later rounds compare the Up next lists each chain reached.")
with table_col:
    st.markdown("**Values**")
    st.dataframe(
        round_frame.reset_index(),
        hide_index=True,
        width="stretch",
        placeholder="",
        column_config={
            "round": st.column_config.TextColumn("Round", width="small"),
            **{column: st.column_config.NumberColumn(column, format="percent") for column in round_frame.columns},
        },
    )

# ---------------------------------------------------------------------- picks per round
st.subheader("Picked video per round")
pick_rows = []
for depth in rows_range:
    row = {"round": f"R{depth}"}
    for value, session in zip(chosen, sessions):
        record = at_depth(value, depth, library.rounds_of(session))
        selected = library.selected_of(record) if record else {}
        title = selected.get("title") or ""
        shared = selected.get("video_id") and all(selected.get("video_id") in picked[other] for other in chosen)
        row[letters[value]] = ("● " if shared else "") + shorten(title, 70)
    pick_rows.append(row)
st.dataframe(
    pd.DataFrame(pick_rows),
    hide_index=True,
    width="stretch",
    column_config={"round": st.column_config.TextColumn("", width="small"),
                   **{letters[value]: st.column_config.TextColumn(letters[value], width="large") for value in chosen}},
)
st.caption("● marks a video that every selected session picked (in any round).")

# ---------------------------------------------------------------------- common videos
st.subheader("Videos every session saw")
common = set.intersection(*observed.values())
if not common:
    st.caption("No video or Short appeared in all selected sessions.")
else:
    details: dict[str, dict[str, Any]] = {}
    first_seen: dict[str, dict[str, str]] = {}
    for value, session in zip(chosen, sessions):
        for record in library.rounds_of(session):
            for item in library.round_items(record):
                video_id = item.get("video_id")
                if video_id not in common:
                    continue
                details.setdefault(video_id, item)
                seen = first_seen.setdefault(video_id, {})
                if letters[value] not in seen:
                    position = item.get("raw_position", item.get("position"))
                    number = record.get("round")
                    row_label = f"R{offsets[value] + number}" if isinstance(number, int) else "R?"
                    seen[letters[value]] = f"{row_label} #{position}" if position else row_label
    rows = []
    for video_id, item in details.items():
        rows.append(
            {
                "thumb": library.thumbnail_url(video_id),
                "title": item.get("title") or video_id,
                "channel": item.get("channel_name") or "",
                "type": "Short" if item.get("label") == "short" else "video",
                "picked_by": "".join(letter for value, letter in letters.items() if video_id in picked[value]),
                **{letters[value]: first_seen.get(video_id, {}).get(letters[value], "") for value in chosen},
                "url": item.get("url") or library.watch_url(video_id),
            }
        )
    rows.sort(key=lambda row: (row["picked_by"] == "", row[letters[chosen[0]]]))
    st.caption(f"{len(rows)} video(s) appeared in every selected session; each column shows where it first appeared.")
    st.dataframe(
        pd.DataFrame(rows[:200]),
        hide_index=True,
        width="stretch",
        row_height=48,
        column_config={
            "thumb": st.column_config.ImageColumn("", width="small"),
            "title": st.column_config.TextColumn("Title", width="large"),
            "channel": st.column_config.TextColumn("Channel", width="medium"),
            "type": st.column_config.TextColumn("Type", width="small"),
            "picked_by": st.column_config.TextColumn("Picked by", width="small"),
            **{letters[value]: st.column_config.TextColumn(letters[value], width="small",
                                                           help=md(shorten(by_id[value]["query"], 60)))
               for value in chosen},
            "url": st.column_config.LinkColumn("Link", display_text="open", width="small"),
        },
    )
