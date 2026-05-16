"""Job detail: artifact viewer for one application."""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from store import db as store_db

st.title("Job detail")

apps = store_db.list_applications(limit=500)
if not apps:
    st.info("No applications yet.")
    st.stop()

# ---- Picker ----
labels = {a.id: f"{a.company} — {a.title} ({a.status})" for a in apps}
default_id = st.session_state.get("selected_app_id")
default_idx = 0
if default_id and default_id in labels:
    default_idx = list(labels.keys()).index(default_id)
sel_id = st.selectbox(
    "Application",
    options=list(labels.keys()),
    format_func=lambda i: labels[i],
    index=default_idx,
)
app = next(a for a in apps if a.id == sel_id)

# ---- Header ----
left, right = st.columns([3, 1])
with left:
    st.markdown(f"### {app.company} — {app.title}")
    st.markdown(f"`{app.status}` · ATS coverage: **{(app.ats_coverage or 0):.0%}** · "
                f"source: `{app.source}` · updated `{app.updated_at:%Y-%m-%d %H:%M}`")
    if app.apply_url:
        st.markdown(f"[Open job posting]({app.apply_url})")
with right:
    if app.estimates_present:
        st.warning("Contains [ESTIMATE] metrics")
    if app.error:
        st.error(app.error)

st.divider()

# ---- Artifacts ----
out = Path(app.output_dir) if app.output_dir else None
if not out or not out.exists():
    st.info("No output folder for this application yet (it may not have reached the render step).")
    st.stop()

tabs = st.tabs(["Resume", "Decisions", "Q&A", "Meta", "Files"])

# Resume tab — try to show PDF; fall back to .md
with tabs[0]:
    pdf = out / "resume.pdf"
    md = out / "resume.md"
    if pdf.exists():
        st.download_button("Download resume.pdf", pdf.read_bytes(), file_name=pdf.name, mime="application/pdf")
        try:
            import base64
            b64 = base64.b64encode(pdf.read_bytes()).decode("ascii")
            st.markdown(
                f'<iframe src="data:application/pdf;base64,{b64}" width="100%" height="900"></iframe>',
                unsafe_allow_html=True,
            )
        except Exception as e:  # noqa: BLE001
            st.warning(f"PDF preview failed: {e}")
    elif md.exists():
        st.markdown(md.read_text(encoding="utf-8"))
    else:
        st.info("No resume artifact yet.")

with tabs[1]:
    decisions = out / "decisions.md"
    if decisions.exists():
        st.markdown(decisions.read_text(encoding="utf-8"))
    else:
        st.info("decisions.md not generated yet.")

with tabs[2]:
    qa_path = out / "qa.json"
    if qa_path.exists():
        try:
            qa = json.loads(qa_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            qa = []
        if not qa:
            st.info("No custom Q&A recorded for this application.")
        for q in qa:
            with st.expander(q.get("question", "(no question)")):
                st.write(q.get("answer") or "_(needs review)_")
                st.caption(f"source: `{q.get('source')}` · confidence: {q.get('confidence', 0):.2f}")
    else:
        st.info("qa.json not generated yet.")

with tabs[3]:
    meta = out / "meta.json"
    if meta.exists():
        st.json(json.loads(meta.read_text(encoding="utf-8")))
    else:
        st.info("meta.json not generated yet.")

with tabs[4]:
    files = sorted(p for p in out.iterdir() if p.is_file())
    for f in files:
        cols = st.columns([4, 1, 1])
        cols[0].text(f.name)
        cols[1].text(f"{f.stat().st_size:,} B")
        cols[2].download_button("download", f.read_bytes(), file_name=f.name, key=f"dl_{f.name}")
