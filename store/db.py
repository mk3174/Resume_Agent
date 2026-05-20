"""SQLAlchemy session helpers + per-job folder writer.

The engine honours these in order of priority:

1. ``$DATABASE_URL`` environment variable (Phase 5 Postgres swap)
2. ``store.db_url`` in ``settings.yaml`` (SQLite default)

To swap SQLite -> Postgres, set
``DATABASE_URL=postgresql+psycopg://user:pw@host:5432/resume_agent`` in your
environment (or via docker-compose) and ensure the optional ``psycopg`` driver
is installed (``pip install psycopg[binary]``). No code changes required —
the ORM models in :mod:`store.models` are dialect-agnostic.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config_loader import PROJECT_ROOT, load_settings
from orchestrator.state import ApplicationState
from store.models import Application, Base

log = logging.getLogger(__name__)


def _resolve_db_url() -> str:
    """Pick a database URL.

    Order: env DATABASE_URL > settings.store.db_url. Relative sqlite paths are
    resolved against the project root so behaviour is the same regardless of
    cwd.
    """
    db_url = os.getenv("DATABASE_URL") or load_settings()["store"]["db_url"]
    if db_url.startswith("sqlite:///"):
        rel = db_url.replace("sqlite:///", "")
        if not rel.startswith("/"):
            db_url = "sqlite:///" + str((PROJECT_ROOT / rel).resolve())
    return db_url


@lru_cache(maxsize=1)
def _engine() -> Engine:
    db_url = _resolve_db_url()
    log.info("DB engine -> %s", _redact(db_url))
    is_sqlite = db_url.startswith("sqlite")
    engine = create_engine(
        db_url,
        future=True,
        pool_pre_ping=not is_sqlite,
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )
    Base.metadata.create_all(engine)
    return engine


def _redact(url: str) -> str:
    """Hide credentials when we log the connection string."""
    return re.sub(r"://([^:/]+):([^@]+)@", r"://\1:***@", url)


@lru_cache(maxsize=1)
def _session_factory():
    return sessionmaker(bind=_engine(), expire_on_commit=False)


def session() -> Session:
    return _session_factory()()


# ---------------------------------------------------------------------------
# Per-job folder
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def slugify(s: str) -> str:
    return _SLUG_RE.sub("-", s).strip("-").lower()


def make_output_dir(state: ApplicationState) -> Path:
    cfg_out = PROJECT_ROOT / load_settings()["store"]["output_dir"]
    today = datetime.utcnow().strftime("%Y%m%d")
    name = f"{today}_{slugify(state.job.company)[:30]}_{slugify(state.job.title)[:40]}"
    out = cfg_out / name
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_artifacts(state: ApplicationState, *, extra: dict[str, str] | None = None) -> None:
    """Write decisions.md, qa.json, meta.json, and remember any provided extras."""
    if not state.output_dir:
        return
    out = Path(state.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Decisions.md
    decisions_path = out / "decisions.md"
    decisions_path.write_text(_decisions_md(state), encoding="utf-8")
    state.artifacts["decisions.md"] = str(decisions_path)

    # qa.json
    qa_path = out / "qa.json"
    qa_path.write_text(
        json.dumps([q.model_dump() for q in state.qa], indent=2), encoding="utf-8"
    )
    state.artifacts["qa.json"] = str(qa_path)

    # meta.json
    meta_path = out / "meta.json"
    meta = {
        "stable_key": state.job.stable_key,
        "company": state.job.company,
        "title": state.job.title,
        "apply_url": str(state.job.apply_url),
        "status": state.status.value,
        "ats_coverage": state.tailored.ats_coverage if state.tailored else None,
        "estimates_present": state.estimates_present,
        "barriers": [b.model_dump() for b in state.barriers],
        "selected_projects": state.tailored.selected_projects if state.tailored else [],
    }
    meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    state.artifacts["meta.json"] = str(meta_path)

    if extra:
        state.artifacts.update(extra)


def _decisions_md(state: ApplicationState) -> str:
    lines: list[str] = [f"# Decisions for {state.job.company} - {state.job.title}", ""]
    lines.append(f"- Apply URL: {state.job.apply_url}")
    lines.append(f"- Status: `{state.status.value}`")
    if state.tailored:
        lines.append(f"- ATS coverage: {state.tailored.ats_coverage:.0%}")
        lines.append(f"- Selected projects: {', '.join(state.tailored.selected_projects) or '(none)'}")
    if state.jd_analysis:
        lines.append("")
        lines.append("## JD must-haves")
        lines.extend(f"- {kw}" for kw in state.jd_analysis.must_have_keywords)
        lines.append("")
        lines.append("## JD themes")
        lines.extend(f"- {t}" for t in state.jd_analysis.semantic_themes)

    if state.estimates_present and state.tailored:
        lines.append("")
        lines.append("## Estimated metrics (review before submit)")
        for e in state.tailored.experience:
            for b in e.bullets:
                if b.has_estimate:
                    lines.append(f"- ({e.company}) {b.text}")
        for b in state.tailored.project_bullets:
            if b.has_estimate:
                lines.append(f"- (project) {b.text}")

    if state.validation_errors:
        lines.append("")
        lines.append("## Validation errors")
        lines.extend(f"- {e}" for e in state.validation_errors)

    if state.barriers:
        lines.append("")
        lines.append("## Barriers")
        for b in state.barriers:
            lines.append(f"- [{b.kind.value}] {b.message}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Tracker upsert
# ---------------------------------------------------------------------------


def upsert(state: ApplicationState, *, error: str | None = None) -> None:
    with session() as s:
        existing = s.scalar(
            select(Application).where(Application.stable_key == state.job.stable_key)
        )
        common: dict[str, Any] = {
            "source": state.job.source.value,
            "company": state.job.company,
            "title": state.job.title,
            "location": state.job.location,
            "apply_url": str(state.job.apply_url),
            "status": state.status.value,
            "ats_coverage": state.tailored.ats_coverage if state.tailored else None,
            "output_dir": state.output_dir,
            "error": error,
            "estimates_present": state.estimates_present,
            "barriers": [b.model_dump(mode="json") for b in state.barriers] or None,
            "meta": {
                "selected_projects": state.tailored.selected_projects if state.tailored else [],
            },
        }
        if existing:
            for k, v in common.items():
                setattr(existing, k, v)
        else:
            s.add(Application(stable_key=state.job.stable_key, **common))
        s.commit()


def list_applications(limit: int = 100) -> list[Application]:
    with session() as s:
        rows = s.execute(
            select(Application).order_by(Application.updated_at.desc()).limit(limit)
        ).scalars().all()
        return list(rows)
