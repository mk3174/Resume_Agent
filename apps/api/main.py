"""FastAPI service layer for Resume Agent (Phase 5).

Exposes the same operations the CLI does so a separate front-end (Next.js, a
mobile companion, or a hosted Streamlit dashboard) can drive the pipeline over
HTTP. The service is intentionally thin: every endpoint delegates to the same
functions the CLI uses.

Run locally:

    uv run uvicorn apps.api.main:app --reload --port 8000

Endpoints
---------

- ``GET  /health``           — same checks as `resume-agent doctor`
- ``GET  /applications``     — list recent applications from the SQLite tracker
- ``GET  /applications/{id}`` — fetch a single application
- ``POST /pipeline/index``   — rebuild the project library vector store
- ``POST /pipeline/ingest``  — pull jobs from all configured boards
- ``POST /pipeline/run``     — run the full pipeline (filter -> tailor -> ...)
- ``GET  /barriers/pending`` — list applications currently blocked on a barrier
- ``POST /barriers/{id}/resolve`` — supply the human answer for a parked barrier

The service shares the in-process LangGraph build, SQLAlchemy engine, and
Chroma client with the CLI — so running the API and the CLI against the same
project root is safe (SQLite WAL mode handles the concurrency).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

app = FastAPI(
    title="Resume Agent API",
    version="0.1.0",
    description="Headless control plane for the Resume Agent pipeline.",
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class HealthCheck(BaseModel):
    ok: bool
    checks: dict[str, dict[str, Any]]


class ApplicationOut(BaseModel):
    id: int
    stable_key: str
    company: str
    title: str
    location: str | None = None
    source: str
    status: str
    ats_coverage: float | None = None
    output_dir: str | None = None
    apply_url: str | None = None
    estimates_present: bool = False
    barriers: list[dict[str, Any]] | None = None


class RunRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=50)
    dry_apply: bool = True


class RunResponse(BaseModel):
    queued: bool
    note: str
    job_count: int


class BarrierResolveRequest(BaseModel):
    answer: str = Field(min_length=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthCheck, tags=["meta"])
def health() -> HealthCheck:
    from apps.streamlit_ui.lib.health import check_all

    checks = check_all()
    payload = {name: {"ok": ok, "message": msg} for name, (ok, msg) in checks.items()}
    return HealthCheck(ok=all(ok for ok, _ in checks.values()), checks=payload)


# ---------------------------------------------------------------------------
# Applications (tracker)
# ---------------------------------------------------------------------------


@app.get("/applications", response_model=list[ApplicationOut], tags=["applications"])
def list_applications(limit: int = 50) -> list[ApplicationOut]:
    from store import db as store_db

    rows = store_db.list_applications(limit=limit)
    return [_to_out(r) for r in rows]


@app.get("/applications/{app_id}", response_model=ApplicationOut, tags=["applications"])
def get_application(app_id: int) -> ApplicationOut:
    from sqlalchemy import select

    from store import db as store_db
    from store.models import Application

    with store_db.session() as s:
        row = s.get(Application, app_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"application {app_id} not found")
        return _to_out(row)


# ---------------------------------------------------------------------------
# Pipeline operations
# ---------------------------------------------------------------------------


@app.post("/pipeline/index", tags=["pipeline"])
def reindex() -> dict[str, Any]:
    from retrieval import project_library

    n = project_library.reindex()
    return {"indexed": n}


@app.post("/pipeline/ingest", tags=["pipeline"])
def ingest_jobs() -> dict[str, Any]:
    from ingest.runner import fetch_all

    jobs = fetch_all()
    return {"count": len(jobs), "sources": _by_source(jobs)}


@app.post("/pipeline/run", response_model=RunResponse, tags=["pipeline"])
def run_pipeline(req: RunRequest, background: BackgroundTasks) -> RunResponse:
    """Kick the full pipeline off in a background thread so the HTTP call returns
    quickly. Status changes land in the SQLite tracker; poll `/applications`.
    """
    from filters import filter_jobs
    from ingest.runner import fetch_all

    jobs = fetch_all()
    kept = filter_jobs(jobs)
    background.add_task(_run_pipeline_background, kept[: req.limit], req.dry_apply)
    return RunResponse(
        queued=True,
        note=f"Pipeline launched for {min(len(kept), req.limit)} of {len(kept)} filtered jobs",
        job_count=min(len(kept), req.limit),
    )


# ---------------------------------------------------------------------------
# Barrier queue
# ---------------------------------------------------------------------------


@app.get("/barriers/pending", response_model=list[ApplicationOut], tags=["barriers"])
def pending_barriers() -> list[ApplicationOut]:
    from sqlalchemy import or_, select

    from store import db as store_db
    from store.models import Application

    with store_db.session() as s:
        rows = s.execute(
            select(Application)
            .where(or_(Application.status == "barrier", Application.status == "needs_review"))
            .order_by(Application.updated_at.desc())
        ).scalars().all()
        return [_to_out(r) for r in rows]


@app.post("/barriers/{app_id}/resolve", tags=["barriers"])
def resolve_barrier(app_id: int, req: BarrierResolveRequest) -> dict[str, Any]:
    """Persist a human-supplied answer for a parked application's outstanding barrier.

    This mirrors what `notify/telegram.py -> barrier_pause` would do interactively.
    Used by external UIs (Next.js, mobile) to clear barriers without going
    through Telegram.
    """
    from datetime import datetime

    from store import db as store_db
    from store.models import Application

    with store_db.session() as s:
        row = s.get(Application, app_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"application {app_id} not found")
        barriers = list(row.barriers or [])
        if not barriers:
            raise HTTPException(status_code=400, detail="no barriers on this application")
        for b in barriers:
            if b.get("resolved_at"):
                continue
            b["human_response"] = req.answer
            b["resolved_at"] = datetime.utcnow().isoformat()
            break
        row.barriers = barriers
        row.status = "ready_to_apply"
        s.commit()
    return {"ok": True, "application_id": app_id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_out(row) -> ApplicationOut:
    return ApplicationOut(
        id=row.id,
        stable_key=row.stable_key,
        company=row.company,
        title=row.title,
        location=row.location,
        source=row.source,
        status=row.status,
        ats_coverage=row.ats_coverage,
        output_dir=row.output_dir,
        apply_url=row.apply_url,
        estimates_present=row.estimates_present,
        barriers=row.barriers,
    )


def _by_source(jobs) -> dict[str, int]:
    out: dict[str, int] = {}
    for j in jobs:
        out[j.source.value] = out.get(j.source.value, 0) + 1
    return out


def _run_pipeline_background(filtered, dry_apply: bool) -> None:
    """Worker function — same shape as `apps.cli.main.run`."""
    from config_loader import load_settings
    from notify.telegram import notify
    from orchestrator.graph import build_graph
    from orchestrator.state import ApplicationState, ApplicationStatus
    from store import db as store_db

    load_settings()["apply"]["dry_apply"] = bool(dry_apply)
    graph = build_graph()
    for job, _ in filtered:
        try:
            state = ApplicationState(job=job)
            result = graph.invoke(state)
            final = ApplicationState(**result) if isinstance(result, dict) else result
            store_db.upsert(final)
            if final.estimates_present:
                notify(
                    f"Estimates present in {job.company} - {job.title}. Review {final.output_dir}/decisions.md",
                    level="info",
                )
            if final.status == ApplicationStatus.NEEDS_REVIEW:
                notify(
                    f"NEEDS REVIEW: {job.company} - {job.title}",
                    level="warn",
                )
        except Exception as e:  # noqa: BLE001
            log.exception("background pipeline failed for %s/%s: %s", job.company, job.title, e)
