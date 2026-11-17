"""Optional start-query ideas: OpenAI Top-5 generation, Google Trends Assist and the prompt editor.

Moved from the original single-page app with the same behaviour; only the layout changed (tabs
inside one expander) and the values now survive switching pages.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from ui.common import SECRETS_HINT, persistent, remembered, set_widget_value, widget_key
from recpath.config import OPENAI_MODEL_OPTIONS, RuntimeConfig
from recpath.openai_queries import QueryGenerationError, generate_queries
from recpath.prompts import SYSTEM_PROMPT, build_query_user_message
from recpath.safety import error_summary
from recpath.trends import (
    TRENDS_PAGE_URL,
    TRENDS_RSS_URL,
    GoogleTrendsError,
    build_google_trends_explore_url,
    fetch_google_trends_rss,
    fetch_google_trends_terms,
    parse_google_trends_csv,
    selected_trend_terms_for_prompt,
)


TREND_SOURCES = ["Interest-based Explore CSV", "Trending Now RSS", "BigQuery public dataset"]
CUSTOM_MODEL_OPTION = "Custom model ID..."


def ensure_state() -> None:
    st.session_state.setdefault("generated_queries", [])
    st.session_state.setdefault("trend_terms", [])
    st.session_state.setdefault("selected_trend_terms", [])
    st.session_state.setdefault("trend_metadata", {})
    st.session_state.setdefault("trend_raw_items", [])
    st.session_state.setdefault("trend_loaded_source_mode", None)


def queries_dataframe(queries: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for item in queries:
        row = item.copy()
        source_terms = row.get("source_terms")
        row["source_terms"] = ", ".join(str(term) for term in source_terms) if isinstance(source_terms, list) else ""
        rows.append(row)
    return pd.DataFrame(rows).reindex(columns=["rank", "query", "reason", "source_terms"])


def trend_terms_dataframe(terms: list[dict[str, Any]]) -> pd.DataFrame:
    columns = ["rank", "term", "trend_type", "date", "score", "source", "source_table", "url"]
    return pd.DataFrame([{column: term.get(column) for column in columns} for term in terms], columns=columns)


def clear_trend_bundle() -> None:
    st.session_state.trend_terms = []
    st.session_state.trend_raw_items = []
    st.session_state.trend_metadata = {}
    st.session_state.selected_trend_terms = []
    st.session_state.trend_loaded_source_mode = None


def store_trend_bundle(bundle: dict[str, Any], *, source_mode: str) -> None:
    st.session_state.trend_terms = bundle.get("terms", [])
    st.session_state.trend_raw_items = bundle.get("raw_items", [])
    st.session_state.trend_metadata = {key: value for key, value in bundle.items() if key not in {"terms", "raw_items"}}
    st.session_state.selected_trend_terms = []
    st.session_state.trend_loaded_source_mode = source_mode


# ---------------------------------------------------------------------- public entry point
def render_query_ideas(config: RuntimeConfig, *, start_query_name: str = "start_query") -> dict[str, Any]:
    """Draw the "Need query ideas?" expander; returns what a new session should record."""
    ensure_state()
    generated = st.session_state.generated_queries
    with st.expander("Need query ideas?", icon=":material/lightbulb:", expanded=bool(generated)):
        st.caption(
            "Optional. Describe an interest, optionally add Google Trends context, and let OpenAI "
            "suggest five concrete start queries. Clicking a suggestion fills the start query above."
        )
        user_prompt = persistent(
            st.text_area,
            "user_prompt",
            "",
            "Interest description",
            height=110,
            placeholder="Find YouTube topics Irish teenagers may currently be interested in...",
        )
        tab_generate, tab_trends, tab_prompt = st.tabs(["Generate with OpenAI", "Google Trends", "System prompt"])
        with tab_generate:
            openai_model = render_model_selector(config)
        with tab_trends:
            selected_trend_terms = render_trend_assist(config, user_prompt)
        with tab_prompt:
            system_prompt = render_prompt_editor(
                config=config,
                openai_model=openai_model,
                user_prompt=user_prompt,
                trend_terms=selected_trend_terms,
            )
        with tab_generate:
            render_generation(
                config=config,
                openai_model=openai_model,
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                trend_terms=selected_trend_terms,
                start_query_name=start_query_name,
            )
    return {
        "user_prompt": user_prompt,
        "system_prompt": system_prompt,
        "openai_model": openai_model,
        "generated_queries": st.session_state.generated_queries,
    }


def render_model_selector(config: RuntimeConfig) -> str:
    model_options = list(dict.fromkeys([config.openai_model, *OPENAI_MODEL_OPTIONS]))
    choice = persistent(
        st.selectbox,
        "openai_model_option",
        model_options[0],
        "OpenAI model",
        [*model_options, CUSTOM_MODEL_OPTION],
        help=(
            "Used only for Top-5 query generation and recorded in the session. "
            "Availability depends on your OpenAI project."
        ),
    )
    if choice == CUSTOM_MODEL_OPTION:
        return persistent(st.text_input, "openai_custom_model", "", "Custom OpenAI model ID",
                          placeholder="Enter an API model ID").strip()
    return choice or model_options[0]


def _fill_start_query(pills_key: str, start_query_name: str) -> None:
    picked = st.session_state.get(pills_key)
    if picked:
        set_widget_value(start_query_name, picked)


def render_generation(
    *,
    config: RuntimeConfig,
    openai_model: str,
    user_prompt: str,
    system_prompt: str,
    trend_terms: list[dict[str, Any]],
    start_query_name: str,
) -> None:
    if not config.openai_api_key:
        st.info(f"Query generation needs an OpenAI API key (`OPENAI_API_KEY`). {SECRETS_HINT}", icon=":material/key:")
    disabled = not user_prompt.strip() or not config.openai_api_key or not system_prompt.strip() or not openai_model
    if st.button("Generate Top-5 queries", type="primary", disabled=disabled, icon=":material/auto_awesome:"):
        try:
            with st.spinner("Generating queries"):
                st.session_state.generated_queries = generate_queries(
                    api_key=config.openai_api_key or "",
                    model=openai_model,
                    user_prompt=user_prompt,
                    trend_terms=trend_terms,
                    region=config.region,
                    language=config.language,
                    system_prompt=system_prompt,
                )
        except QueryGenerationError as exc:
            st.error(error_summary(exc))
        except Exception as exc:
            st.error(f"Query generation failed: {error_summary(exc)}")
    if config.openai_api_key and not user_prompt.strip():
        st.caption("Enter an interest description first.")

    queries = st.session_state.generated_queries
    if not queries:
        return
    options = [item["query"] for item in queries if item.get("query")]
    pills_key = "generated_query_pick"
    if st.session_state.get(pills_key) not in options or st.session_state.get(pills_key) != remembered(start_query_name):
        st.session_state[pills_key] = None
    st.pills(
        "Suggested queries (click one to use it)",
        options,
        key=pills_key,
        on_change=_fill_start_query,
        args=(pills_key, start_query_name),
    )
    st.dataframe(
        queries_dataframe(queries),
        hide_index=True,
        width="stretch",
        column_config={
            "rank": st.column_config.NumberColumn("Rank", width="small"),
            "query": st.column_config.TextColumn("Query", width="medium"),
            "reason": st.column_config.TextColumn("Reason", width="large"),
            "source_terms": st.column_config.TextColumn("Source terms", width="medium"),
        },
    )
    if st.button("Clear suggestions", type="tertiary", icon=":material/close:"):
        st.session_state.generated_queries = []
        st.rerun()


def _restore_default_prompt() -> None:
    set_widget_value("system_prompt", SYSTEM_PROMPT)


def render_prompt_editor(
    *,
    config: RuntimeConfig,
    openai_model: str,
    user_prompt: str,
    trend_terms: list[dict[str, Any]],
) -> str:
    st.caption(
        "The text below is the exact system message sent to the LLM. "
        "You can edit it; the Top-5 JSON structure is still required by the app."
    )
    st.button("Restore default system prompt", on_click=_restore_default_prompt, icon=":material/restart_alt:")
    active_prompt = persistent(
        st.text_area,
        "system_prompt",
        SYSTEM_PROMPT,
        "System prompt",
        height=360,
        help=(
            "Personalize the query-generation rules here. Keep the instruction to "
            "return exactly five items and the documented JSON shape."
        ),
    )
    if active_prompt.strip() != SYSTEM_PROMPT.strip():
        st.info("A customized system prompt will be used for the next generation request.")
    if not active_prompt.strip():
        st.warning("The system prompt is empty, so query generation is disabled.")

    user_message = build_query_user_message(
        user_prompt=user_prompt,
        trend_terms=trend_terms,
        region=config.region,
        language=config.language,
        query_count=5,
    )
    st.markdown("**Exact user message preview**")
    st.code(user_message, language="text")
    st.markdown("**Request settings**")
    st.json(
        {
            "model": openai_model,
            "response_format": "json_object",
            "region": config.region,
            "language": config.language,
            "selected_trend_term_count": len(trend_terms),
        }
    )
    return active_prompt


def render_trend_assist(config: RuntimeConfig, user_prompt: str) -> list[dict[str, Any]]:
    st.caption(
        "The default flow searches Google Trends Explore with your exact interest "
        "text, Ireland, the last 7 days, and the YouTube Search property."
    )
    source_mode = persistent(
        st.radio,
        "trend_source_mode",
        TREND_SOURCES[0],
        "Trend data source",
        TREND_SOURCES,
        horizontal=True,
        help=(
            "Explore CSV is based on your text. RSS and BigQuery are region-wide "
            "sources and are explicitly marked as not interest-based."
        ),
    )

    loaded_source_mode = st.session_state.trend_loaded_source_mode
    if loaded_source_mode and loaded_source_mode != source_mode:
        clear_trend_bundle()

    seed_query = " ".join(user_prompt.split())
    explore_url = build_google_trends_explore_url(seed_query, region=config.region, language=config.language)
    loaded_seed = st.session_state.trend_metadata.get("seed_query")
    if source_mode == TREND_SOURCES[0] and loaded_seed and loaded_seed != seed_query:
        clear_trend_bundle()

    try:
        if source_mode == TREND_SOURCES[0]:
            st.markdown("**Exact Trend search seed**")
            st.code(seed_query or "No interest description entered yet.", language="text")
            if not seed_query:
                st.info(
                    "Enter an interest description above. The Explore link and CSV "
                    "upload will then be tied to that exact text."
                )
            else:
                st.caption("Official Google Trends Explore URL")
                st.code(explore_url, language="text")
                st.link_button("Search this text in Google Trends", explore_url, icon=":material/open_in_new:")
                st.write(
                    "On Google Trends, inspect Related queries, export the relevant "
                    "Top or Rising table as CSV, then upload the unchanged file below."
                )
            uploaded_file = st.file_uploader(
                "Interest-based Google Trends CSV",
                type=["csv"],
                help=(
                    "The uploaded file is parsed locally. It is accepted only while "
                    "an interest description is present, and that seed is recorded."
                ),
            )
            if st.button("Use interest-based CSV", disabled=uploaded_file is None or not seed_query, type="primary"):
                store_trend_bundle(
                    parse_google_trends_csv(
                        uploaded_file.getvalue(),
                        region=config.region,
                        limit=25,
                        filename=uploaded_file.name,
                        seed_query=seed_query,
                        source_page_url=explore_url,
                    ),
                    source_mode=source_mode,
                )

        elif source_mode == TREND_SOURCES[1]:
            st.warning(
                "This source is not based on your interest text. It is the general "
                "Ireland Trending Now list and is retained only as an optional baseline."
            )
            source_url = TRENDS_RSS_URL.format(region=config.region)
            st.link_button("Open Ireland Trending Now", TRENDS_PAGE_URL.format(region=config.region),
                           icon=":material/open_in_new:")
            st.caption("Official Trending Now RSS feed URL")
            st.code(source_url, language="text")
            if st.button("Fetch official RSS", type="primary"):
                with st.spinner("Fetching the official Google Trends RSS feed"):
                    store_trend_bundle(fetch_google_trends_rss(region=config.region, limit=25), source_mode=source_mode)

        else:
            st.warning(
                "This source is not based on your interest text. It queries the "
                "latest available Ireland rows in the public dataset."
            )
            st.write(
                "BigQuery discovers a compatible table, filters it to Ireland and "
                "the latest available date, then normalizes the returned rows."
            )
            if not config.gcp_credentials_path:
                st.info(
                    "Set `GCP_CREDENTIALS_PATH` in `.streamlit/secrets.toml` or the "
                    "environment (or pass `--gcp-credentials-path`) to enable this source."
                )
            if st.button("Fetch from BigQuery", disabled=not config.gcp_credentials_path, type="primary"):
                with st.spinner("Fetching Google Trends BigQuery context"):
                    store_trend_bundle(
                        fetch_google_trends_terms(
                            credentials_path=config.gcp_credentials_path or "",
                            project_id=config.gcp_project_id,
                            region=config.region,
                            limit=25,
                        ),
                        source_mode=source_mode,
                    )
    except GoogleTrendsError as exc:
        st.warning(error_summary(exc))
    except Exception as exc:
        st.warning(f"Google Trends fetch failed: {error_summary(exc)}")

    trend_terms = st.session_state.trend_terms
    if trend_terms:
        st.markdown("**1. Source and fetch details**")
        st.json(st.session_state.trend_metadata)

        raw_items = st.session_state.trend_raw_items
        show_raw = st.checkbox(
            f"Show {len(raw_items)} raw source row(s)",
            value=False,
            help="These rows are shown before deduplication or field normalization.",
        )
        if show_raw:
            st.dataframe(pd.DataFrame(raw_items), width="stretch", hide_index=True)

        st.markdown("**2. Normalized terms**")
        st.dataframe(
            trend_terms_dataframe(trend_terms),
            width="stretch",
            hide_index=True,
            column_config={"url": st.column_config.LinkColumn("url")},
        )
        options = [term["term"] for term in trend_terms if term.get("term")]
        default = [term for term in st.session_state.selected_trend_terms if term in options]
        selected = st.multiselect(
            "3. Trend terms to include in LLM query generation",
            options,
            default=default,
            help=(
                "Selected trend terms are added as context for Top-5 query generation. "
                "They do not change browser tracking directly."
            ),
        )
        st.session_state.selected_trend_terms = selected

    selected_terms = selected_trend_terms_for_prompt(st.session_state.trend_terms, st.session_state.selected_trend_terms)
    if selected_terms:
        st.success(f"{len(selected_terms)} trend term(s) will be included in the visible LLM prompt.")
    return selected_terms


__all__ = ["render_query_ideas", "ensure_state", "widget_key"]
