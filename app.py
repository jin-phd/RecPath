"""RecPath: Streamlit entry point.

    streamlit run app.py
    streamlit run app.py -- --max-rounds 3   # optional flags, see docs/REFERENCE.md

The pages live in ui/pages; crawls run in the background through recpath.run_manager.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st


# `streamlit run` puts this folder on sys.path; make the ui/ and recpath/ packages importable
# for other launchers too (e.g. streamlit.testing.AppTest started from another directory).
PROJECT_ROOT = str(Path(__file__).resolve().parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

st.set_page_config(page_title="RecPath", page_icon=":material/smartphone:", layout="wide")
# A little less empty space above each page title (the navigation sits in the top bar).
st.html("<style>[data-testid='stMainBlockContainer']{padding-top:4.5rem;padding-bottom:3rem}</style>")

PAGES = [
    st.Page("ui/pages/new_run.py", title="New run", icon=":material/add_circle:", default=True),
    st.Page("ui/pages/live_run.py", title="Live run", icon=":material/radio_button_checked:"),
    st.Page("ui/pages/library.py", title="Session library", icon=":material/video_library:"),
    st.Page("ui/pages/compare.py", title="Compare", icon=":material/compare_arrows:"),
]

st.navigation(PAGES, position="top").run()
