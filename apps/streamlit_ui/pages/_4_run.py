"""Run pipeline: kick off ingest / run from the UI with live progress."""

from __future__ import annotations

import time
from pathlib import Path

import streamlit as st

from filters import filter_jobs
from ingest.runner import fetch_all
from orchestrator.graph import build_graph
from orchestrator.state import ApplicationState, ApplicationStatus
from store import db as store_db

st.title("Run pipeline")
st.caption("Same as the CLI's `resume-agent run`, with a knob and a live log. "
           "Heavy LLM work happens here — keep the tab open.")

col1, col2, col3 = st.columns([1, 1, 2])
limit = col1.slider("Max jobs", 1, 25, 3)
dry_apply = col2.toggle("Dry-apply (Phase 1 default)", value=True)
ingest_only = col3.toggle("Ingest only (no LLM tailoring)", value=False)

if "run_log" not in st.session_state:
    st.session_state["run_log"] = []

if st.button("Start", type="primary"):
    log_box = st.empty()

    def log(msg: str) -> None:
        st.session_state["run_log"].append(msg)
        log_box.code("\n".join(st.session_state["run_log"][-200:]))

    st.session_state["run_log"] = []

    log("Ingesting jobs from configured ATS boards...")
    t0 = time.time()
    jobs = fetch_all()
    log(f"  fetched {len(jobs)} jobs in {time.time() - t0:.1f}s")

    if ingest_only:
        log("Ingest-only mode: skipping filter + tailor.")
        st.success(f"Done. {len(jobs)} jobs cached in memory.")
        st.stop()

    log("Filtering jobs against preferences (semantic + rules)...")
    t0 = time.time()
    kept = filter_jobs(jobs)
    log(f"  kept {len(kept)} jobs in {time.time() - t0:.1f}s")

    if not kept:
        st.warning("No jobs passed the filter. Loosen preferences.yaml.")
        st.stop()

    graph = build_graph()
    bar = st.progress(0.0, text="Tailoring...")
    summary = []
    n = min(limit, len(kept))
    for i, (job, score) in enumerate(kept[:limit], 1):
        log(f"[{i}/{n}] {job.company} — {job.title}  (semantic={score:.2f})")
        t0 = time.time()
        state = ApplicationState(job=job)
        try:
            result = graph.invoke(state)
            final = ApplicationState(**result) if isinstance(result, dict) else result
        except Exception as e:  # noqa: BLE001
            log(f"   ERROR: {e}")
            summary.append((job.company, job.title, "error"))
            continue
        store_db.upsert(final)
        log(
            f"   -> {final.status.value} "
            f"(ATS {(final.tailored.ats_coverage if final.tailored else 0):.0%}, "
            f"{time.time() - t0:.1f}s)"
        )
        summary.append((job.company, job.title, final.status.value))
        bar.progress(i / n, text=f"Tailoring {i}/{n}")

    bar.empty()
    st.success(f"Done. Processed {len(summary)} jobs.")
    st.dataframe(
        {"Company": [s[0] for s in summary],
         "Title": [s[1] for s in summary],
         "Status": [s[2] for s in summary]},
        use_container_width=True,
    )
elif st.session_state["run_log"]:
    st.code("\n".join(st.session_state["run_log"][-200:]))
