"""Resume Agent — Streamlit dashboard.

Run via:
    uv run streamlit run apps/streamlit_ui/app.py

Multipage layout (st.navigation), with a sidebar showing live system status.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Allow running with `streamlit run apps/streamlit_ui/app.py` from project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

st.set_page_config(
    page_title="Resume Agent",
    page_icon=":briefcase:",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ---------------------------------------------------------------------------
# Sidebar: live system status
# ---------------------------------------------------------------------------


def _sidebar_status() -> None:
    from apps.streamlit_ui.lib.health import check_all

    st.sidebar.markdown("### System status")
    checks = check_all()
    for name, (ok, msg) in checks.items():
        icon = "🟢" if ok else "🔴"
        st.sidebar.markdown(f"{icon} **{name}** — {msg}")
    st.sidebar.divider()
    st.sidebar.caption(f"Project: `{PROJECT_ROOT.name}`")


_sidebar_status()


# ---------------------------------------------------------------------------
# Multipage navigation
# ---------------------------------------------------------------------------


pages = [
    st.Page("pages/_1_dashboard.py", title="Dashboard", icon=":material/dashboard:"),
    st.Page("pages/_2_job_detail.py", title="Job detail", icon=":material/description:"),
    st.Page("pages/_3_inputs.py", title="Inputs", icon=":material/upload_file:"),
    st.Page("pages/_4_run.py", title="Run pipeline", icon=":material/play_circle:"),
]
nav = st.navigation(pages)
nav.run()
