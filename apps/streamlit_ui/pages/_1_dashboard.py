"""Dashboard: applications table + summary stats."""

from __future__ import annotations

from collections import Counter

import pandas as pd
import streamlit as st

from store import db as store_db

st.title("Dashboard")
st.caption("Every job the pipeline has touched, with status and ATS coverage.")

apps = store_db.list_applications(limit=500)

if not apps:
    st.info("No applications yet. Run the pipeline from the **Run pipeline** page.")
    st.stop()

# ---- Summary stats ----
status_counts = Counter(a.status for a in apps)
cols = st.columns(6)
cols[0].metric("Total", len(apps))
cols[1].metric("Ready", status_counts.get("ready_to_apply", 0))
cols[2].metric("Applied", status_counts.get("applied", 0))
cols[3].metric("Needs review", status_counts.get("needs_review", 0))
cols[4].metric("Barriers", status_counts.get("barrier", 0))
cols[5].metric("Failed", status_counts.get("failed", 0))

st.divider()

# ---- Filters ----
fcol1, fcol2, fcol3 = st.columns([2, 2, 1])
status_filter = fcol1.multiselect(
    "Status",
    options=sorted({a.status for a in apps}),
    default=[],
)
company_query = fcol2.text_input("Company contains", "")
estimates_only = fcol3.checkbox("Estimates only", value=False)

rows = []
for a in apps:
    if status_filter and a.status not in status_filter:
        continue
    if company_query and company_query.lower() not in (a.company or "").lower():
        continue
    if estimates_only and not a.estimates_present:
        continue
    rows.append(
        {
            "Company": a.company,
            "Title": a.title,
            "Source": a.source,
            "Status": a.status,
            "ATS": f"{(a.ats_coverage or 0):.0%}" if a.ats_coverage is not None else "-",
            "Estimates": "Y" if a.estimates_present else "",
            "Updated": a.updated_at.strftime("%Y-%m-%d %H:%M"),
            "Output": a.output_dir or "",
            "_id": a.id,
        }
    )

df = pd.DataFrame(rows)
st.caption(f"{len(df)} applications shown")

if not df.empty:
    selected = st.dataframe(
        df.drop(columns=["_id"]),
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
    )
    sel_rows = selected.selection.get("rows", []) if hasattr(selected, "selection") else []
    if sel_rows:
        sel_id = int(df.iloc[sel_rows[0]]["_id"])
        st.session_state["selected_app_id"] = sel_id
        st.success(f"Selected app id={sel_id}. Open the **Job detail** page to inspect.")
