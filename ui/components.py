"""Reusable view pieces: phone frame, badges, round cards, path graph, tables, ads, Shorts."""
from __future__ import annotations

import base64
import html
import io
import re
import textwrap
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import streamlit as st

from ui.common import (
    LABEL_COLORS,
    NAVIGATION_DONE,
    STATUS_COLORS,
    compact_number,
    duration_label,
    label_name,
    md,
    shorten,
    source_label,
    type_name,
)
from recpath import library
from recpath.ads import distinct_player_ads


# ---------------------------------------------------------------------- images
@st.cache_data(show_spinner=False, max_entries=600)
def _thumb_bytes(path: str, mtime_ns: int, width: int) -> bytes | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB")
            if image.width > width:
                image.thumbnail((width, int(width * image.height / max(image.width, 1))))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=80)
            return buffer.getvalue()
    except Exception:
        return None


def thumb_bytes(path: Path | str | None, width: int = 360) -> bytes | None:
    """A small JPEG of a saved screenshot (legacy PNGs are ~1.6 MB each)."""
    if not path:
        return None
    file = Path(path)
    try:
        mtime = file.stat().st_mtime_ns
    except OSError:
        return None
    return _thumb_bytes(str(file), mtime, width)


def data_url(data: bytes | None, mime: str = "image/jpeg") -> str | None:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}" if data else None


# ---------------------------------------------------------------------- phone frame & status
PHONE_CSS = """
<style>
.vt-phone{width:min(var(--vt-w),100%);margin:0 auto;padding:12px 9px 16px;border-radius:40px;
  background:#16181d;box-shadow:0 12px 32px rgba(15,23,42,.18);box-sizing:border-box}
.vt-phone .vt-notch{width:34%;height:16px;background:#000;border-radius:0 0 12px 12px;margin:-12px auto 6px}
.vt-phone img{width:100%;display:block;border-radius:24px;background:#fff}
.vt-phone .vt-empty{aspect-ratio:9/19;border-radius:24px;background:#22252c;color:#9aa3ad;display:flex;
  align-items:center;justify-content:center;text-align:center;font:14px/1.4 sans-serif;padding:24px}
.vt-caption{text-align:center;font:13px/1.4 sans-serif;color:#5b6573;margin-top:8px}
.vt-dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:baseline}
.vt-dot.live{background:#d93025;animation:vt-blink 1.2s infinite}
.vt-dot.done{background:#1e8e3e}.vt-dot.error{background:#d93025}.vt-dot.idle{background:#9aa3ad}
.vt-dot.stopped{background:#3b6ea5}
.vt-live{font:600 13px/1.4 sans-serif;color:#b3261e;letter-spacing:.02em}
@keyframes vt-blink{50%{opacity:.25}}
</style>
"""


def status_dot(state: str) -> str:
    """HTML for a small coloured dot: live | done | error | stopped | idle."""
    return f'<span class="vt-dot {html.escape(state)}"></span>'


def phone_frame(
    image: bytes | Path | str | None,
    *,
    caption_html: str = "",
    width: int = 300,
    placeholder: str = "Waiting for the first frame…",
) -> None:
    """A CSS phone bezel around a screenshot or a live frame (JPEG bytes or a file path)."""
    data = image if isinstance(image, (bytes, bytearray)) else thumb_bytes(image, 520) if image else None
    url = data_url(bytes(data)) if data else None
    inner = f'<img src="{url}" alt="phone screen"/>' if url else f'<div class="vt-empty">{html.escape(placeholder)}</div>'
    caption = f'<div class="vt-caption">{caption_html}</div>' if caption_html else ""
    st.html(f'{PHONE_CSS}<div class="vt-phone" style="--vt-w:{int(width)}px"><div class="vt-notch"></div>{inner}</div>{caption}')


def status_badge(status: str | None) -> None:
    value = status or "unknown"
    st.badge(value, color=STATUS_COLORS.get(value, "gray"))


def label_chips(counts: dict[str, int], *, order: Iterable[str] = ("normal_video", "short", "ad", "live", "playlist", "mix", "movie", "channel", "upcoming")) -> None:
    """One small badge per card type in a feed, e.g. "20 videos", "26 Shorts"."""
    keys = [key for key in order if counts.get(key)] + sorted(key for key in counts if key not in set(order) and counts[key])
    if not keys:
        st.caption("No cards recorded.")
        return
    with st.container(horizontal=True, gap="small"):
        for key in keys:
            st.badge(f"{counts[key]} {label_name(key, counts[key])}", color=LABEL_COLORS.get(key, "gray"))


# ---------------------------------------------------------------------- chain cards
def chain_cards(round_records: list[dict[str, Any]], *, active_round: int | None = None, width: int = 190) -> None:
    """The chain so far as compact horizontal cards: round, picked video thumbnail and title."""
    if not round_records:
        st.caption("The first round has not finished yet.")
        return
    with st.container(horizontal=True, gap="small"):
        for record in round_records:
            selected = library.selected_of(record)
            number = record.get("round")
            with st.container(border=True, width=width, gap="small"):
                st.badge(
                    f"R{number} · {source_label(record.get('source'))}",
                    color="red" if number == active_round else "gray",
                )
                thumb = library.thumbnail_url(selected.get("video_id"))
                if thumb:
                    st.image(thumb, width="stretch")
                    st.markdown(f"**{md(shorten(selected.get('title') or selected.get('video_id'), 70))}**")
                    channel = selected.get("channel_name")
                    position = library.selected_position(record)
                    bits = [md(shorten(channel, 30)) if channel else None, f"#{position}" if position else None]
                    st.caption(" · ".join(bit for bit in bits if bit) or " ")
                else:
                    st.caption("No video selected in this round.")


# ---------------------------------------------------------------------- path graph
def _dot_text(value: Any, width: int = 22, lines: int = 3) -> str:
    text = " ".join(str(value or "").split()).replace("\\", "/").replace('"', "'")
    wrapped = textwrap.wrap(text, width)[:lines] or [""]
    if len(textwrap.wrap(text, width)) > lines:
        wrapped[-1] = wrapped[-1][: max(width - 1, 1)] + "…"
    return "\\n".join(wrapped)


def chain_dot(
    session: dict[str, Any],
    children: list[dict[str, Any]] | None = None,
    *,
    grandchildren: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    """DOT source: start query -> pick of each round; child sessions hang off as dashed branches.

    A child started from round r with a different video is drawn as an alternative pick of that
    round; a continuation (same video as round r's pick) continues from the pick itself.
    """
    lines = [
        "digraph chain {",
        'graph [rankdir=LR, bgcolor="transparent", nodesep=0.2, ranksep=0.35, pad=0.15];',
        'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, margin="0.14,0.07", '
        'color="#c5ccd6", fillcolor="#ffffff", fontcolor="#1f2933"];',
        'edge [fontname="Helvetica", fontsize=9, color="#8a94a3", fontcolor="#5b6573", arrowsize=0.7];',
    ]
    counter = {"n": 0}

    def node_id() -> str:
        counter["n"] += 1
        return f"n{counter['n']}"

    def draw(sess: dict[str, Any], entry: str | None, depth: int) -> dict[int, str]:
        ids: dict[int, str] = {}
        previous = entry
        for record in library.rounds_of(sess):
            selected = library.selected_of(record)
            nid = node_id()
            ids[int(record.get("round") or 0)] = nid
            title = selected.get("title") or selected.get("video_id")
            channel = selected.get("channel_name")
            label = f"R{record.get('round')}\\n{_dot_text(title, 18) if title else 'no pick'}"
            if channel:
                label += f"\\n({_dot_text(channel, 20, 1)})"
            fill = "#ffffff" if selected else "#fff6dd"
            border = "#c5ccd6" if depth == 0 else "#8fb0d9"
            lines.append(f'{nid} [label="{label}", fillcolor="{fill}", color="{border}"];')
            if previous:
                position = library.selected_position(record)
                count = record.get("raw_result_count")
                edge = f"#{position}" if position else ""
                if position and count:
                    edge += f"/{count}"
                if record.get("source") == "search_results":
                    edge = f"search {edge}".strip()
                lines.append(f'{previous} -> {nid} [label=" {edge}"];')
            previous = nid
        return ids

    root = node_id()
    query = library.start_query(session) or "(start)"
    lines.append(f'{root} [label="Search\\n{_dot_text(query, 20, 3)}", fillcolor="#eef3fb", color="#2e5e8c"];')
    main_ids = draw(session, root, 0)

    def attach(parent_session: dict[str, Any], parent_ids: dict[int, str], kids: list[dict[str, Any]], depth: int) -> None:
        rounds = {int(item.get("round") or 0): item for item in library.rounds_of(parent_session)}
        for child in kids:
            parent = child.get("parent") or {}
            parent_round = parent.get("round") if isinstance(parent.get("round"), int) else None
            if parent_round is None:
                continue
            pick = library.selected_of(rounds.get(parent_round, {}))
            if parent.get("video_id") and parent.get("video_id") == pick.get("video_id") and parent_round in parent_ids:
                anchor = parent_ids[parent_round]
                edge_label = "continued"
            else:
                before = parent_ids.get(parent_round - 1, root if depth == 0 else None) if parent_round > 1 else root
                if before is None:
                    continue
                anchor = node_id()
                lines.append(
                    f'{anchor} [label="R{parent_round} alt\\n{_dot_text(parent.get("title") or parent.get("video_id"), 18)}", '
                    'fillcolor="#eef3fb", color="#2e5e8c", style="rounded,filled,dashed"];'
                )
                lines.append(f'{before} -> {anchor} [style=dashed, color="#2e5e8c", label=" branch"];')
                edge_label = "branch"
            child_ids = draw(child, None, depth + 1)
            if child_ids:
                first = child_ids[min(child_ids)]
                lines.append(f'{anchor} -> {first} [style=dashed, color="#2e5e8c", label=" {edge_label}"];')
            nested = (grandchildren or {}).get(str(child.get("session_id")), [])
            if nested and depth < 2:
                attach(child, child_ids, nested, depth + 1)

    attach(session, main_ids, children or [], 0)
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- candidates
def parse_view_count_text(text: Any) -> int | None:
    """Approximate views from YouTube's short text ("279K views", "1.2M views", "1,234 views")."""
    if not isinstance(text, str):
        return None
    match = re.search(r"([\d.,]+)\s*([KMB]?)", text.replace("\u00a0", " "), re.IGNORECASE)
    if not match:
        return 0 if text.lower().startswith("no views") else None
    number = float(match.group(1).replace(",", "")) if match.group(2) else float(match.group(1).replace(",", ""))
    scale = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2).upper()]
    return int(round(number * scale))

def candidates_frame(session: dict[str, Any], round_numbers: Iterable[int] | None = None) -> pd.DataFrame:
    wanted = set(round_numbers) if round_numbers is not None else None
    rows: list[dict[str, Any]] = []
    for record in library.rounds_of(session):
        number = record.get("round")
        if wanted is not None and number not in wanted:
            continue
        selected_id = library.selected_of(record).get("video_id")
        excluded = set(record.get("filtered_out_video_ids") or [])
        # Display ranks are numbered on the selectable list (results), not on the raw feed.
        ranks = {
            result.get("video_id"): result.get("display_rank")
            for result in record.get("results") or []
            if isinstance(result, dict) and result.get("video_id") and result.get("display_rank")
        }
        for item in library.round_items(record):
            video_id = item.get("video_id")
            rows.append(
                {
                    "round": number,
                    "picked": bool(video_id) and video_id == selected_id,
                    "thumb": library.thumbnail_url(video_id),
                    "position": item.get("raw_position", item.get("position")),
                    "rank": item.get("display_rank") or ranks.get(video_id),
                    "type": type_name(item.get("label")),
                    "label": item.get("label") or "other",
                    "title": item.get("title") or "",
                    "channel": item.get("channel_name") or "",
                    "length": item.get("duration_text") or duration_label(item.get("duration_seconds")),
                    "views": (
                        item.get("view_count") if isinstance(item.get("view_count"), (int, float))
                        else parse_view_count_text((item.get("innertube") or {}).get("view_count_text"))
                    ),
                    "seen_before": bool(video_id) and video_id in excluded,
                    "metadata": item.get("metadata_source") or "",
                    "presentation": item.get("presentation") or "",
                    "shorts_evidence": [str(value) for value in item.get("shorts_evidence") or [] if value],
                    "duration_bucket": item.get("duration_bucket") or "",
                    "renderer": (item.get("innertube") or {}).get("renderer") or ""
                    if isinstance(item.get("innertube"), dict) else "",
                    "video_id": video_id or "",
                    "url": item.get("url") or item.get("canonical_url") or library.watch_url(video_id),
                }
            )
    return pd.DataFrame(
        rows,
        columns=[
            "round", "picked", "thumb", "position", "rank", "type", "label", "title", "channel", "length",
            "views", "seen_before", "metadata", "presentation", "shorts_evidence", "duration_bucket", "renderer",
            "video_id", "url",
        ],
    )


CANDIDATE_COLUMN_CONFIG = {
    "round": st.column_config.NumberColumn("Round", width="small"),
    "picked": st.column_config.CheckboxColumn("Picked", width="small", help="The video the chain selected"),
    "thumb": st.column_config.ImageColumn("", width="small"),
    "position": st.column_config.NumberColumn("Pos", width="small", help="Order on the page before any filtering"),
    "rank": st.column_config.NumberColumn("Rank", width="small", help="Rank among the selectable normal videos"),
    "type": st.column_config.TextColumn("Type", width="small"),
    "title": st.column_config.TextColumn("Title", width="large"),
    "channel": st.column_config.TextColumn("Channel", width="medium"),
    "length": st.column_config.TextColumn("Length", width="small"),
    "views": st.column_config.NumberColumn("Views", format="compact", width="small"),
    "seen_before": st.column_config.CheckboxColumn(
        "Seen", width="small", help="Excluded because an earlier round already showed or watched it"
    ),
    "metadata": st.column_config.TextColumn("Metadata", width="small", help="browser_dom or youtube_api"),
    "presentation": st.column_config.TextColumn("Shown as", width="small"),
    "shorts_evidence": st.column_config.ListColumn(
        "Shorts evidence", width="medium",
        help="Signals that labelled the card a Short: dom:shorts_link, dom:shorts_card, dom:shorts_badge, "
        "innertube:reel_endpoint, or YouTube's own answer for /shorts/<id> (shorts_url:200 or shorts_url:redirect).",
    ),
    "duration_bucket": st.column_config.TextColumn(
        "Length bucket", width="small", help="<=60s, 61-180s or >180s (a Short can be up to 3 minutes)"
    ),
    "renderer": st.column_config.TextColumn(
        "YT renderer", width="small", help="The InnerTube renderer YouTube used to draw the card"
    ),
    "url": st.column_config.LinkColumn("Link", display_text="open", width="small"),
}


def candidate_columns(frame: pd.DataFrame, *, with_round: bool = True, compact: bool = False) -> list[str]:
    """Columns worth showing for ``frame``; Phase 2 columns only when some item has them.

    ``compact`` (the round dialog) puts the title right after the Picked box, so it stays
    visible on a narrow screen; the thumbnail moves behind the text columns.
    """
    columns = ["round"] if with_round else []
    columns.append("picked")
    if compact:
        columns += ["title", "type", "position"]
    else:
        columns += ["thumb", "position"]
    if frame["rank"].notna().any():
        columns.append("rank")
    if not compact:
        columns += ["type", "title"]
    columns += ["channel", "length"]
    if frame["views"].notna().any():
        columns.append("views")
    columns.append("seen_before")
    if (frame["presentation"] != "").any():
        columns.append("presentation")
    if frame["shorts_evidence"].map(bool).any():
        columns.append("shorts_evidence")
    if (frame["duration_bucket"] != "").any():
        columns.append("duration_bucket")
    if (frame["renderer"] != "").any():
        columns.append("renderer")
    if compact:
        columns.append("thumb")
    columns += ["metadata", "url"]
    return columns


# ---------------------------------------------------------------------- round facts
def _fact(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "—"
    return str(value)


def round_facts(record: dict[str, Any]) -> list[dict[str, str]]:
    """What the runner recorded about a round's page and navigation, as (fact, value) rows.

    Only facts the record has are listed, so an older session does not show a wall of dashes.
    """
    rows: list[dict[str, str]] = []

    def add(name: str, value: Any) -> None:
        rows.append({"fact": name, "value": _fact(value)})

    if "page_url" in record:
        add("Page URL", record.get("page_url"))
    if "current_video_id" in record:
        add("Video on the page", record.get("current_video_id"))
    add("Source", source_label(record.get("source")))
    location = record.get("location") if isinstance(record.get("location"), dict) else None
    if location:
        add(
            "Content location: requested → applied (PREF)",
            f"{_fact(location.get('requested'))} → {_fact(location.get('gl'))} ({_fact(location.get('pref_gl'))})",
        )
    counts = [record.get(name) for name in ("raw_result_count", "filtered_result_count", "filtered_duplicate_count")]
    if any(value is not None for value in counts):
        add("Cards: observed / selectable / already seen", " / ".join(_fact(value) for value in counts))
    if "feed_scope_found" in record:
        add("Recommendation list found", record.get("feed_scope_found"))
    if "stale_card_count" in record:
        add("Stale cards ignored", record.get("stale_card_count"))
    scroll = record.get("scroll") if isinstance(record.get("scroll"), dict) else None
    if scroll:
        add(
            "Scrolling",
            f"{_fact(scroll.get('passes'))} {'pass' if scroll.get('passes') == 1 else 'passes'} · "
            f"stalled {_fact(scroll.get('stalled'))} · "
            f"cap reached {_fact(scroll.get('cap_reached'))}",
        )
    navigation = record.get("navigation") if isinstance(record.get("navigation"), dict) else None
    if navigation:
        method = navigation.get("method")
        add("Next video opened by", NAVIGATION_DONE.get(method, method) if method else None)
        add("Requested → landed video", f"{_fact(navigation.get('requested_video_id'))} → {_fact(navigation.get('landed_video_id'))}")
        if navigation.get("fallback_reason"):
            add("Tap fallback reason", navigation.get("fallback_reason"))
        elapsed = navigation.get("elapsed_ms")
        if isinstance(elapsed, (int, float)):
            add("Navigation time", f"{elapsed / 1000:.1f} s")
        if navigation.get("error"):
            add("Navigation error", navigation.get("error"))
    elif "navigation" in record:
        add("Next video opened by", "not opened (last round or nothing to pick)")
    if "metadata_enriched" in record:
        add("YouTube Data API enrichment", record.get("metadata_enriched"))
    if record.get("metadata_enrichment_error"):
        add("API enrichment error", record.get("metadata_enrichment_error"))
    return rows


def round_record_without_items(record: dict[str, Any]) -> dict[str, Any]:
    """The round record minus its (long) item lists, for a JSON view."""
    return {key: value for key, value in record.items() if key not in {"raw_results", "results"}}


# ---------------------------------------------------------------------- ad counts
def ads_tile(count: Any, scope: str | None) -> tuple[str, Any, str]:
    """(label, value, help) for an ad-count tile that says what the number covers."""
    if scope == library.ADS_SCOPE_FEED_ONLY:
        return (
            "Feed ads",
            count,
            "Only ad cards inside the feeds were labelled. This session did not observe the video player, "
            "so player ads are unknown, not zero.",
        )
    if scope == library.ADS_SCOPE_SHORTS:
        return ("Ads seen", count, "Ads met while swiping through the Shorts feed")
    return ("Ads seen", count, "Ad cards in the feeds plus ads seen in the video player; blank when ads were not recorded")


# ---------------------------------------------------------------------- ads
def _player_ads(ads: dict[str, Any]) -> list[dict[str, Any]]:
    player = ads.get("player") if isinstance(ads.get("player"), dict) else {}
    items = player.get("ads") if isinstance(player.get("ads"), list) else []
    return distinct_player_ads(items)


def _beacon_text(beacons: Any) -> tuple[int | None, str]:
    """(total, "stats_ads 2 · activeview 3") for a beacon-count dict."""
    if not isinstance(beacons, dict):
        return None, ""
    counts = {str(name): int(value) for name, value in beacons.items() if isinstance(value, (int, float)) and value}
    return sum(counts.values()), " · ".join(f"{name} {value}" for name, value in counts.items())


def _has_value(value: Any) -> bool:
    """False for None, NaN (a missing number in a DataFrame) and empty text."""
    return value is not None and value == value and value != ""


def ads_rows(session: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for record in library.rounds_of(session):
        ads = library.ads_of_round(record) or {}
        counts = library.label_counts(record)
        player = ads.get("player") if isinstance(ads.get("player"), dict) else {}
        companion = ads.get("companion") if isinstance(ads.get("companion"), dict) else {}
        scheduled = [item for item in (ads.get("scheduled") or []) if isinstance(item, dict)]
        player_ads = _player_ads(ads)
        first_ms = player.get("first_seen_ms")
        beacon_total, beacon_detail = _beacon_text(ads.get("beacons"))
        ad_ids = sorted({str(value) for item in scheduled for value in (item.get("ad_video_ids") or []) if value})
        texts = [str(text) for item in scheduled for text in (item.get("texts") or []) if text]
        rows.append(
            {
                "round": record.get("round"),
                "page": source_label(record.get("source")),
                "feed_ad_cards": counts.get("ad", 0),
                # None (not "no") where the player was not watched, e.g. on a search page.
                "player_ad": bool(player.get("ad_seen") or player_ads) if library.player_observed(record) else None,
                "beacons": beacon_total,
                "first_seen_s": round(first_ms / 1000, 1) if isinstance(first_ms, (int, float)) else None,
                "player_ads": "; ".join(
                    shorten(" · ".join(str(part) for part in (ad.get("title"), ad.get("pod_text")) if part), 90)
                    for ad in player_ads
                ),
                "advertiser": ", ".join(sorted(
                    {str(ad.get("advertiser_host")) for ad in player_ads if ad.get("advertiser_host")}
                    | ({str(companion["advertiser"])} if companion.get("advertiser") else set())
                )),
                "skippable": None if not player_ads else any(ad.get("skippable") for ad in player_ads),
                "companion": shorten(
                    " · ".join(
                        str(part)
                        for part in dict.fromkeys(
                            (companion.get("headline"), companion.get("advertiser") or companion.get("cta_host"))
                        )
                        if part
                    ),
                    80,
                ),
                "scheduled": ", ".join(
                    f"{item.get('kind') or 'ad'}"
                    + (f" @{round(item['offset_ms'] / 1000)}s" if isinstance(item.get("offset_ms"), (int, float)) else "")
                    for item in scheduled
                ),
                "scheduled_ad_ids": ", ".join(ad_ids),
                "scheduled_texts": shorten(" · ".join(dict.fromkeys(texts)), 120),
                "beacon_detail": beacon_detail,
                "paid_promotion": ads.get("paid_promotion") if ads else None,
                "observed_s": round(ads["observed_ms"] / 1000, 1) if isinstance(ads.get("observed_ms"), (int, float)) else None,
            }
        )
    return rows


ADS_COLUMN_CONFIG = {
    "round": st.column_config.NumberColumn("Round", width="small"),
    "page": st.column_config.TextColumn("Page", width="small"),
    "feed_ad_cards": st.column_config.NumberColumn("Feed ad cards", width=95),
    "player_ad": st.column_config.CheckboxColumn("Player ad", width=75),
    "first_seen_s": st.column_config.NumberColumn("First seen (s)", width=95),
    "player_ads": st.column_config.TextColumn("Player ads", width="large"),
    "advertiser": st.column_config.TextColumn("Advertiser", width="medium"),
    "skippable": st.column_config.CheckboxColumn("Skippable", width="small"),
    "companion": st.column_config.TextColumn("Companion", width="medium"),
    "scheduled": st.column_config.TextColumn("Scheduled", width="medium", help="Ad slots announced by the player"),
    "scheduled_ad_ids": st.column_config.TextColumn(
        "Scheduled ad videos", width="medium", help="Video ids of the ads YouTube scheduled for this video"
    ),
    "scheduled_texts": st.column_config.TextColumn("Scheduled ad texts", width="medium"),
    "beacons": st.column_config.NumberColumn(
        "Ad beacons", width="small", help="Ad-related requests the page made during the round (impression and view pings)"
    ),
    "beacon_detail": st.column_config.TextColumn("Beacons by kind", width="medium"),
    "paid_promotion": st.column_config.CheckboxColumn("Paid promotion", width="small"),
    "observed_s": st.column_config.NumberColumn("Watched (s)", width="small", help="How long the player was observed"),
}


def shorts_ads_panel(session: dict[str, Any]) -> None:
    """Ads met in a Shorts feed session: the ad items and the page's ad beacons."""
    items = library.shorts_items(session)
    shorts_ads = [item for item in items if item.get("is_ad")]
    if not items:
        st.caption("This Shorts feed session recorded no items.")
        return
    st.caption(f"{len(shorts_ads)} of {len(items)} Shorts feed items were ads.")
    total, detail = _beacon_text(session.get("ad_beacons"))
    if total is not None:
        st.caption(
            f"Ad beacons during the feed: {total}" + (f" ({md(detail)})" if detail else "")
            + ". These are the ad impression and view requests the page made."
        )
    if shorts_ads:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "position": item.get("position"),
                        "advertiser": (item.get("ad") or {}).get("advertiser") or "",
                        "headline": (item.get("ad") or {}).get("headline") or "",
                        "cta_host": (item.get("ad") or {}).get("cta_host") or "",
                        "dwell_s": round(item["dwell_ms"] / 1000, 1) if isinstance(item.get("dwell_ms"), (int, float)) else None,
                    }
                    for item in shorts_ads
                ]
            ),
            column_config={
                "position": st.column_config.NumberColumn("#", width="small"),
                "advertiser": st.column_config.TextColumn("Advertiser", width="medium"),
                "headline": st.column_config.TextColumn("Headline", width="large"),
                "cta_host": st.column_config.TextColumn("Link host", width="medium"),
                "dwell_s": st.column_config.NumberColumn("Watched (s)", width="small"),
            },
            hide_index=True,
            width="stretch",
        )


def ads_panel(session: dict[str, Any]) -> None:
    if library.session_mode(session) == library.MODE_SHORTS:
        shorts_ads_panel(session)
        return
    rounds = library.rounds_of(session)
    has_round_ads = any(library.player_observed(item) for item in rounds)
    feed_ad_items = [
        (record.get("round"), item)
        for record in rounds
        for item in library.round_items(record)
        if item.get("label") == "ad"
    ]
    shorts_ads = [item for item in library.shorts_items(session) if item.get("is_ad")]
    if not rounds and not shorts_ads:
        st.caption("This session has no rounds.")
        return
    if not has_round_ads:
        st.info(
            "This session did not observe the video player, so player ads are unknown (not zero). "
            "Sessions recorded with the newer runner keep an ads record per round (player ads, companion, "
            "scheduled slots, ad beacons); older ones only label ad cards inside the feeds.",
            icon=":material/info:",
        )
    rows = ads_rows(session)
    if rows and (has_round_ads or any(row["feed_ad_cards"] for row in rows)):
        frame = pd.DataFrame(rows)
        # Columns no round has a value for are left out (older ads records lack beacons and
        # scheduled ids; search rounds have no player), so the wide table stays readable.
        always = {"round", "page", "feed_ad_cards", "player_ad"}
        columns = [
            column for column in frame.columns
            if (has_round_ads or column in {"round", "page", "feed_ad_cards"})
            and (column in always or frame[column].map(_has_value).any())
        ]
        st.dataframe(frame, column_order=columns, column_config=ADS_COLUMN_CONFIG, hide_index=True, width="stretch",
                     placeholder="")
    if has_round_ads:
        shots = [
            (record.get("round"), library.round_screenshots(record).get("ad"))
            for record in rounds
        ]
        shots = [(number, path) for number, path in shots if path]
        if shots:
            st.markdown("**Player ad frames**")
            with st.container(horizontal=True, gap="small"):
                for number, path in shots:
                    with st.container(width=170, gap="small"):
                        data = thumb_bytes(path, 340)
                        if data:
                            st.image(data, width="stretch")
                        st.caption(f"Round {number}")
    if feed_ad_items:
        st.markdown("**Ad cards in the feeds**")
        st.dataframe(
            pd.DataFrame(
                [
                    {"round": number, "position": item.get("raw_position", item.get("position")),
                     "title": item.get("title") or "", "url": item.get("url")}
                    for number, item in feed_ad_items
                ]
            ),
            column_config={
                "round": st.column_config.NumberColumn("Round", width="small"),
                "position": st.column_config.NumberColumn("Pos", width="small"),
                "title": st.column_config.TextColumn("Ad text", width="large"),
                "url": st.column_config.LinkColumn("Link", display_text="open", width="small"),
            },
            hide_index=True,
            width="stretch",
        )
    if shorts_ads:
        st.markdown("**Ads in the Shorts feed**")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "position": item.get("position"),
                        "advertiser": (item.get("ad") or {}).get("advertiser") or "",
                        "headline": (item.get("ad") or {}).get("headline") or "",
                        "cta_host": (item.get("ad") or {}).get("cta_host") or "",
                    }
                    for item in shorts_ads
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    elif not has_round_ads and not feed_ad_items and rows:
        st.caption("No ad cards were labelled in the observed feeds.")


# ---------------------------------------------------------------------- Shorts filmstrip
def shorts_filmstrip(items: list[dict[str, Any]], *, card_width: int = 150, limit: int | None = None) -> None:
    if not items:
        st.caption("No Shorts recorded yet.")
        return
    shown = items[-limit:] if limit else items
    with st.container(horizontal=True, gap="small"):
        for item in shown:
            with st.container(border=True, width=card_width, gap="small"):
                path = library.resolve_screenshot(item.get("screenshot"))
                data = thumb_bytes(path, 300) if path else None
                if data:
                    st.image(data, width="stretch")
                elif item.get("video_id"):
                    st.image(library.thumbnail_url(item.get("video_id")), width="stretch")
                position = item.get("position")
                if item.get("is_ad"):
                    st.badge(f"#{position} · ad", color="orange")
                    ad = item.get("ad") or {}
                    st.caption(md(shorten(ad.get("advertiser") or ad.get("headline") or ad.get("cta_host") or "Sponsored", 60)))
                else:
                    st.badge(f"#{position}", color="violet")
                    st.caption(md(shorten(item.get("title") or item.get("video_id") or "", 60)))
                    if item.get("channel_name"):
                        st.caption(md(shorten(item.get("channel_name"), 30)))
                dwell = item.get("dwell_ms")
                if isinstance(dwell, (int, float)):
                    st.caption(f"watched {dwell / 1000:.1f} s")


def shorts_frame(items: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "position": item.get("position"),
                "thumb": library.thumbnail_url(item.get("video_id")),
                "is_ad": bool(item.get("is_ad")),
                "title": item.get("title") or ((item.get("ad") or {}).get("headline") if item.get("is_ad") else "") or "",
                "channel": item.get("channel_name") or ((item.get("ad") or {}).get("advertiser") if item.get("is_ad") else "") or "",
                "dwell_s": round(item["dwell_ms"] / 1000, 1) if isinstance(item.get("dwell_ms"), (int, float)) else None,
                "url": item.get("url"),
            }
            for item in items
        ],
        columns=["position", "thumb", "is_ad", "title", "channel", "dwell_s", "url"],
    )


SHORTS_COLUMN_CONFIG = {
    "position": st.column_config.NumberColumn("#", width="small"),
    "thumb": st.column_config.ImageColumn("", width="small"),
    "is_ad": st.column_config.CheckboxColumn("Ad", width="small"),
    "title": st.column_config.TextColumn("Title / headline", width="large"),
    "channel": st.column_config.TextColumn("Channel / advertiser", width="medium"),
    "dwell_s": st.column_config.NumberColumn("Watched (s)", width="small"),
    "url": st.column_config.LinkColumn("Link", display_text="open", width="small"),
}


# ---------------------------------------------------------------------- metrics
def metric_row(items: list[tuple[str, Any, str | None]]) -> None:
    """Bordered metric tiles in one row: (label, value, help)."""
    with st.container(horizontal=True, gap="small"):
        for label, value, help_text in items:
            st.metric(label, "—" if value is None else value, help=help_text, border=True)


def views_text(value: Any) -> str:
    return compact_number(value)
