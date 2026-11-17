"""New run: choose what to observe and start it in the background."""
from __future__ import annotations

import streamlit as st

from ui.common import (
    CONSENT_LABELS,
    EXCLUDE_POLICY_LABELS,
    LOCATION_LABELS,
    NAVIGATION_LABELS,
    PAGE_LIVE,
    MAX_ROUNDS_LIMIT,
    SECRETS_HINT,
    STRATEGY_LABELS,
    default_rounds,
    get_config,
    md,
    persistent,
    remembered,
    shorten,
)
from ui.query_ideas import render_query_ideas
from ui.run_options import browser_options, default_dwell_seconds
from recpath import browser_observation as bo
from recpath import run_manager
from recpath.library import MODE_API, MODE_CHAIN, MODE_SHORTS
from recpath.safety import error_summary


MODE_CHOICES = {
    MODE_CHAIN: "Recommendation chain",
    MODE_SHORTS: "Shorts feed",
    MODE_API: "API metadata (keyword chain)",
}
MODE_HELP = {
    MODE_CHAIN: (
        "Searches the query on m.youtube.com in a phone-emulated Chromium, picks a video from the "
        "results, then follows the Up next recommendations round by round."
    ),
    MODE_SHORTS: "Searches the query, opens the Shorts player and swipes through the Shorts feed, recording each item and any ads.",
    MODE_API: (
        "Secondary mode without a browser: searches the YouTube Data API, then searches keywords "
        "derived from each pick. It is not YouTube's recommendation feed."
    ),
}
FIELDS = run_manager.settings_field_names()

config = get_config()

st.title("New run")
location = LOCATION_LABELS.get(remembered("content_location", "IE"), "Ireland (IE)")
st.caption(
    "Phone-emulated Chromium on m.youtube.com · a fresh anonymous visitor every run · "
    f"this computer's IP · content location {location} · English"
)

active = run_manager.current()
if active is not None:
    with st.container(border=True):
        st.markdown(
            f"**A run is in progress:** {md(shorten(active.params.get('start_query'), 60))} "
            f"(started {active.started_label()}). Only one run can drive the browser at a time."
        )
        st.page_link(PAGE_LIVE, label="Watch it on Live run", icon=":material/radio_button_checked:")

mode = persistent(
    st.segmented_control,
    "run_mode",
    MODE_CHAIN,
    "Run type",
    list(MODE_CHOICES),
    format_func=MODE_CHOICES.get,
    required=True,
) or MODE_CHAIN
shorts_supported = run_manager.shorts_feed_available()
playwright_ok = bo.playwright_available()

with st.container(border=True):
    st.caption(MODE_HELP[mode])
    start_query = persistent(
        st.text_input,
        "start_query",
        "",
        "Start query",
        placeholder="What would a viewer type into YouTube? e.g. Transition Year vlog Ireland",
    ).strip()
    ideas = render_query_ideas(config)

    if mode == MODE_SHORTS:
        shorts_items = persistent(
            st.number_input,
            "shorts_items",
            10,
            "Shorts to watch",
            min_value=1,
            max_value=50,
            step=1,
            help="How many items of the Shorts feed to record (ads in the feed count as items).",
        )
        strategy = "top_1"
        max_rounds = 1
        scan_depth = 20
        api_results = 50
    else:
        col_strategy, col_rounds, col_depth = st.columns([1.1, 1, 1.4], vertical_alignment="bottom")
        with col_strategy:
            strategy = persistent(
                st.segmented_control,
                "strategy",
                "top_1",
                "Pick strategy",
                list(STRATEGY_LABELS),
                format_func=STRATEGY_LABELS.get,
                required=True,
                help="Top result: the first selectable normal video. Random: any selectable normal video (the seed is recorded).",
            ) or "top_1"
        with col_rounds:
            max_rounds = int(
                persistent(
                    st.number_input,
                    "rounds",
                    default_rounds(config),
                    "Rounds",
                    min_value=1,
                    max_value=MAX_ROUNDS_LIMIT,
                    step=1,
                    help="Round 1 reads the search results; every later round reads the Up next list of the previous pick.",
                )
            )
            if int(config.max_rounds) > MAX_ROUNDS_LIMIT:
                st.caption(f"--max-rounds {int(config.max_rounds)} is above this page's limit; {MAX_ROUNDS_LIMIT} is used.")
        with col_depth:
            if mode == MODE_API:
                api_results = persistent(
                    st.slider,
                    "api_results",
                    50,
                    "API results per round",
                    min_value=5,
                    max_value=50,
                    step=5,
                    help="Search results the YouTube Data API fetches per round before filtering (API maximum: 50).",
                )
                scan_depth = 20
            else:
                scan_depth = persistent(
                    st.select_slider,
                    "scan_depth",
                    20,
                    "Scan depth per round",
                    options=[5, 10, 15, 20, 25, 30, 35, 40],
                    help=(
                        "How many normal videos to read from each feed (scrolling to load more) before "
                        "filtering and selection. Shorts, ads and other cards on the way are recorded too."
                    ),
                )
                api_results = 50
        shorts_items = 10

    if mode in (MODE_CHAIN, MODE_SHORTS):
        with st.expander("Browser settings", icon=":material/tune:"):
            left, right = st.columns(2, gap="large")
            with left:
                persistent(
                    st.selectbox,
                    "device",
                    bo.DEVICE_OPTIONS[0],
                    "Device profile",
                    list(bo.DEVICE_OPTIONS),
                    help="Pixel 7 matches the Chromium engine that actually runs; iPhone 13 sends an iPhone user agent from Chromium.",
                )
                persistent(
                    st.selectbox,
                    "content_location",
                    bo.LOCATION_OPTIONS[0],
                    "Content location",
                    list(bo.LOCATION_OPTIONS),
                    format_func=LOCATION_LABELS.get,
                    help=(
                        "YouTube's Location setting (gl), set before the run like a viewer choosing it in "
                        "YouTube's settings. The IP address, language and time zone stay this computer's; "
                        "every round records the location YouTube actually applied."
                    ),
                )
                persistent(
                    st.segmented_control,
                    "consent_choice",
                    bo.CONSENT_OPTIONS[0],
                    "Cookie consent answer",
                    list(bo.CONSENT_OPTIONS),
                    format_func=CONSENT_LABELS.get,
                    required=True,
                    help="How the EU cookie dialog is answered. This changes personalisation and ads.",
                )
                if mode == MODE_CHAIN:
                    persistent(
                        st.segmented_control,
                        "navigation",
                        bo.NAVIGATION_OPTIONS[0],
                        "Open the selected video by",
                        list(bo.NAVIGATION_OPTIONS),
                        format_func=NAVIGATION_LABELS.get,
                        required=True,
                        help="Tapping keeps YouTube's own navigation; it falls back to the URL if the tap fails.",
                    )
                    persistent(
                        st.segmented_control,
                        "exclude_policy",
                        bo.EXCLUDE_POLICIES[0],
                        "Exclude in later rounds",
                        list(bo.EXCLUDE_POLICIES),
                        format_func=EXCLUDE_POLICY_LABELS.get,
                        required=True,
                        help=(
                            "Everything displayed: a video already shown can never be picked again. "
                            "Only selected videos: just the chain itself is excluded."
                        ),
                    )
            with right:
                if "watch_dwell_ms" in FIELDS:
                    persistent(
                        st.slider,
                        "watch_dwell_s",
                        default_dwell_seconds(),
                        "Watch time per video (s)" if mode == MODE_CHAIN else "Watch time per Short (s)",
                        min_value=0.0,
                        max_value=30.0,
                        step=0.5,
                        help="How long each opened video stays on screen before its page is read (the player is observed for ads meanwhile).",
                    )
                persistent(
                    st.slider,
                    "pacing_ms",
                    250,
                    "Pacing between browser actions (ms)",
                    min_value=0,
                    max_value=1000,
                    step=50,
                    help="A pause Playwright adds before every browser action, for slow, human-readable runs.",
                )
                persistent(
                    st.toggle,
                    "visible_window",
                    False,
                    "Also open a visible browser window",
                    help="Shows the Chromium window on this computer while the run is mirrored here.",
                )
                if config.youtube_api_key:
                    persistent(
                        st.toggle,
                        "api_enrichment",
                        True,
                        "Enrich with the YouTube Data API",
                        help="Corrects titles, channels, publish dates, durations and counts via videos.list. The browser still decides order and clicks.",
                    )
                else:
                    st.caption(
                        "YouTube Data API enrichment is off: no `YOUTUBE_API_KEY` is configured. "
                        "Metadata then comes from the page itself."
                    )
                if "check_shorts_urls" in FIELDS:
                    persistent(
                        st.toggle,
                        "check_shorts_urls",
                        bool(run_manager.settings_field_default("check_shorts_urls", False)),
                        "Verify Shorts by their URL",
                        help="Checks ambiguous cards against their /shorts/ link, so Shorts are labelled from evidence.",
                    )
                if "capture_innertube" in FIELDS:
                    persistent(
                        st.toggle,
                        "capture_innertube",
                        bool(run_manager.settings_field_default("capture_innertube", False)),
                        "Capture YouTube's own card data",
                        help="Also records the InnerTube data the page uses to draw each card (channel ids, view text, renderer).",
                    )

    problems: list[str] = []
    if not start_query:
        problems.append("Enter a start query.")
    if active is not None:
        problems.append("Wait for the active run to finish (or stop it on Live run).")
    if mode == MODE_SHORTS and not shorts_supported:
        problems.append(
            "Shorts feed mode needs the updated browser runner (run_shorts_feed), which this installation does not have yet."
        )
    if mode in (MODE_CHAIN, MODE_SHORTS) and not playwright_ok:
        problems.append(
            "Playwright is not installed: run `python -m pip install -r requirements.txt`, then the "
            "browser install command from step 1 of the README."
        )
    if mode == MODE_API and not config.youtube_api_key:
        problems.append(f"API metadata mode needs a YouTube API key (`YOUTUBE_API_KEY`). {SECRETS_HINT}")

    button_col, note_col = st.columns([1, 3], vertical_alignment="center")
    with button_col:
        start = st.button(
            "Start run",
            type="primary",
            icon=":material/play_arrow:",
            disabled=bool(problems),
            width="stretch",
        )
    with note_col:
        if problems:
            st.caption(" ".join(problems))
        elif mode == MODE_SHORTS:
            st.caption(f"Records {shorts_items} Shorts for “{md(start_query)}”.")
        else:
            st.caption(f"Runs {max_rounds} round(s) for “{md(start_query)}”, then saves to the Session library.")

if start and not problems:
    options = browser_options(config)
    started = False
    try:
        if mode == MODE_CHAIN:
            settings = run_manager.build_settings(
                **{
                    **options,
                    "strategy": strategy,
                    "max_rounds": max_rounds,
                    "max_candidates": scan_depth,
                    "language": config.language,
                }
            )
            run_manager.start_chain(
                start_query=start_query,
                settings=settings,
                generated_queries=ideas["generated_queries"],
                selected_query=start_query,
                system_prompt=ideas["system_prompt"],
                user_prompt=ideas["user_prompt"],
                openai_model=ideas["openai_model"],
            )
        elif mode == MODE_SHORTS:
            settings = run_manager.build_settings(
                **{
                    **{key: value for key, value in options.items() if key not in {"navigation", "exclude_policy"}},
                    "language": config.language,
                }
            )
            run_manager.start_shorts_feed(start_query=start_query, settings=settings, items=int(shorts_items))
        else:
            run_manager.start_api_chain(
                youtube_api_key=config.youtube_api_key or "",
                entry_value=start_query,
                strategy=strategy,
                max_rounds=max_rounds,
                max_results=int(api_results),
                region=config.region,
                language=config.language,
                user_prompt=ideas["user_prompt"],
                generated_queries=ideas["generated_queries"],
                selected_query=start_query,
                system_prompt=ideas["system_prompt"],
                openai_model=ideas["openai_model"],
            )
        started = True
    except run_manager.RunAlreadyActive as exc:
        st.error(md(error_summary(exc)))
    except Exception as exc:
        st.error(f"Could not start the run: {md(error_summary(exc))}")
    if started:
        st.switch_page(PAGE_LIVE)
