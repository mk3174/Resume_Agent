"""Inputs viewer: master resume, projects, preferences."""

from __future__ import annotations

from pathlib import Path

import streamlit as st
import yaml

from config_loader import PROJECT_ROOT, load_personal_facts, load_preferences
from retrieval import project_library
from retrieval.resume_kb import parse_master_resume

st.title("Inputs")
st.caption("View what the agent currently knows. Edit files directly on disk; "
           "run `resume-agent index` after changing the project library.")

tabs = st.tabs(["Master resume", "Project library", "Preferences", "Personal facts"])

# ---- Master resume ----
with tabs[0]:
    master_path = PROJECT_ROOT / "data" / "input" / "master_resume.md"
    st.caption(f"Source: `{master_path.relative_to(PROJECT_ROOT)}`")
    if not master_path.exists():
        st.warning("master_resume.md not found. Run `resume-agent init` to scaffold one.")
    else:
        try:
            master = parse_master_resume()
            st.subheader(master.name)
            st.text(master.headline)
            st.write(master.summary)
            st.markdown("**Skills**: " + ", ".join(master.skills))
            st.markdown("**Experience**")
            for e in master.experience:
                st.markdown(f"- **{e.company}** — {e.title} ({e.start} – {e.end or 'Present'})")
                for b in e.base_bullets:
                    st.caption(f"    · {b}")
        except Exception as e:  # noqa: BLE001
            st.error(f"Parse error: {e}")
        with st.expander("Raw markdown"):
            st.code(master_path.read_text(encoding="utf-8"), language="markdown")

# ---- Project library ----
with tabs[1]:
    projects = project_library.load_projects_from_disk()
    st.metric("Projects on disk", len(projects))
    if not projects:
        st.info(
            "No projects found. Add Markdown files to `data/input/projects/*.md` "
            "(see the example files), then run `resume-agent index`."
        )
    for p in projects:
        with st.expander(f"{p.title} (`{p.id}`)"):
            st.write(p.summary)
            if p.tech_stack:
                st.caption("**Tech**: " + ", ".join(p.tech_stack))
            if p.impact_metrics:
                st.markdown("**Impact**")
                for m in p.impact_metrics:
                    st.markdown(f"- {m}")
            if p.url:
                st.markdown(f"[Source]({p.url})")
            st.divider()
            st.markdown(p.body)

# ---- Preferences ----
with tabs[2]:
    prefs = load_preferences()
    st.json(prefs)

# ---- Personal facts ----
with tabs[3]:
    facts = load_personal_facts()
    st.caption("Used by the Responder to answer custom application questions WITHOUT pinging Telegram.")
    st.json(facts)
