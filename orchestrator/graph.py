"""LangGraph wiring for the per-application pipeline.

Phase 1 graph (apply step is dry-run only):

    tailor -> validate -> [retry tailor up to N times] -> render -> persist -> END

The graph is intentionally per-job. Bulk orchestration lives in apps/cli/main.py
which loops through filtered jobs and runs this graph for each.

Adding LangGraph here costs us ~1 dependency but gives us:
  - Conditional edges (validate -> retry vs proceed)
  - SqliteSaver checkpoints (resume after a barrier without re-tailoring)
  - A clean place to insert Phase 3 apply / barrier nodes later.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langgraph.graph import END, StateGraph

import apply  # registers all per-portal appliers via side-effect import
from apply.base import ApplyContext, patchright_available
from apply.rate_limit import can_apply, remaining
from config_loader import PROJECT_ROOT, load_settings
from orchestrator.state import (
    ApplicationState,
    ApplicationStatus,
    Barrier,
    BarrierKind,
)
from pipeline import tailor as tailor_mod
from pipeline import validate as validate_mod
from render import typst as typst_mod
from retrieval.project_library import query_top_k
from retrieval.resume_kb import parse_master_resume
from store import db as store_db

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def node_select_candidates(state: ApplicationState) -> dict[str, Any]:
    top_k = int(load_settings()["tailor"]["top_k_projects"])
    candidates = query_top_k(state.job.description_text or state.job.title, k=top_k)
    return {"candidate_projects": candidates}


def node_tailor(state: ApplicationState) -> dict[str, Any]:
    master = parse_master_resume()
    try:
        jd, tailored = tailor_mod.tailor(state.job, master, state.candidate_projects)
    except Exception as e:  # noqa: BLE001
        log.exception("tailor failed")
        barrier = Barrier(
            kind=BarrierKind.UNKNOWN,
            message=f"tailor crashed: {e}",
            context={"job": state.job.stable_key},
        )
        return {
            "barriers": state.barriers + [barrier],
            "status": ApplicationStatus.FAILED,
        }
    estimates_present = _estimates_present(tailored)
    return {
        "jd_analysis": jd,
        "tailored": tailored,
        "estimates_present": estimates_present,
        "status": ApplicationStatus.TAILORED,
    }


def node_validate(state: ApplicationState) -> dict[str, Any]:
    if state.tailored is None or state.jd_analysis is None:
        return {}
    master = parse_master_resume()
    ok, errors, tailored = validate_mod.validate(state.tailored, state.jd_analysis, master)
    if ok:
        return {
            "tailored": tailored,
            "validation_errors": [],
            "status": ApplicationStatus.READY_TO_APPLY,
        }
    return {
        "tailored": tailored,
        "validation_errors": errors,
        "retry_count": state.retry_count + 1,
    }


def node_park_for_review(state: ApplicationState) -> dict[str, Any]:
    barrier = Barrier(
        kind=BarrierKind.ATS_FAIL,
        message="validation failed after max retries",
        context={"errors": state.validation_errors},
    )
    return {
        "status": ApplicationStatus.NEEDS_REVIEW,
        "barriers": state.barriers + [barrier],
    }


def node_render(state: ApplicationState) -> dict[str, Any]:
    if state.tailored is None:
        return {}
    master = parse_master_resume()
    out_dir = store_db.make_output_dir(state)
    artifacts = typst_mod.render_pdf(state.tailored, master, out_dir)
    arts = {k: str(v) for k, v in artifacts.items()}
    # Also dump the tailored markdown blob for human review.
    md_path = out_dir / "resume.md"
    md_path.write_text(_to_markdown(state), encoding="utf-8")
    arts["resume.md"] = str(md_path)
    cover_path = out_dir / "cover.md"
    cover_path.write_text(state.tailored.cover_paragraph + "\n", encoding="utf-8")
    arts["cover.md"] = str(cover_path)
    return {
        "output_dir": str(out_dir),
        "artifacts": {**state.artifacts, **arts},
    }


def node_apply(state: ApplicationState) -> dict[str, Any]:
    """Submit (or dry-submit) the application via the per-source applier."""
    settings = load_settings()
    dry = bool(settings["apply"].get("dry_apply", True))

    # Rate limit only applies to real submits.
    if not dry and not can_apply():
        b = Barrier(
            kind=BarrierKind.UNKNOWN,
            message=f"hourly apply cap reached (remaining={remaining()})",
            context={"job": state.job.stable_key},
        )
        return {"barriers": state.barriers + [b], "status": ApplicationStatus.BARRIER}

    applier = apply.get_applier(state.job.source)
    if applier is None:
        b = Barrier(
            kind=BarrierKind.UNKNOWN,
            message=f"no applier registered for source={state.job.source.value}",
        )
        return {"barriers": state.barriers + [b], "status": ApplicationStatus.NEEDS_REVIEW}

    if state.tailored is None or not state.output_dir:
        return {"status": ApplicationStatus.NEEDS_REVIEW}

    if not patchright_available():
        from apply.base import _PATCHRIGHT_INSTALL_HINT

        log.warning("%s", _PATCHRIGHT_INSTALL_HINT)
        if dry:
            # Artifacts (PDF, cover) are done; skip browser screenshot/submit.
            return {"status": ApplicationStatus.READY_TO_APPLY}
        b = Barrier(
            kind=BarrierKind.UNKNOWN,
            message=_PATCHRIGHT_INSTALL_HINT,
            context={"job": state.job.stable_key},
        )
        return {"barriers": state.barriers + [b], "status": ApplicationStatus.NEEDS_REVIEW}

    master = parse_master_resume()
    out_dir = Path(state.output_dir)
    ctx = ApplyContext(
        job=state.job,
        master=master,
        tailored=state.tailored,
        resume_pdf=Path(state.artifacts.get("resume.pdf", out_dir / "resume.pdf")),
        cover_md=Path(state.artifacts.get("cover.md", out_dir / "cover.md")),
        output_dir=out_dir,
        dry_apply=dry,
    )
    try:
        result = applier.apply(ctx)
    except Exception as e:  # noqa: BLE001
        log.exception("applier crashed")
        b = Barrier(
            kind=BarrierKind.UNKNOWN,
            message=f"applier crashed: {e}",
            context={"job": state.job.stable_key},
        )
        return {"barriers": state.barriers + [b], "status": ApplicationStatus.FAILED}

    new_artifacts = dict(state.artifacts)
    if result.screenshot_path:
        new_artifacts["submission_screenshot.png"] = result.screenshot_path

    if result.barriers:
        return {
            "qa": state.qa + result.qa,
            "artifacts": new_artifacts,
            "barriers": state.barriers + result.barriers,
            "status": ApplicationStatus.BARRIER,
        }
    return {
        "qa": state.qa + result.qa,
        "artifacts": new_artifacts,
        "status": ApplicationStatus.APPLIED if result.submitted else ApplicationStatus.READY_TO_APPLY,
    }


def node_persist(state: ApplicationState) -> dict[str, Any]:
    store_db.write_artifacts(state)
    store_db.upsert(state)
    return {}


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------


def route_after_validate(state: ApplicationState) -> str:
    if state.status == ApplicationStatus.READY_TO_APPLY:
        return "render"
    max_retries = int(load_settings()["tailor"].get("max_retailor_attempts", 2))
    if state.retry_count > max_retries:
        return "park_for_review"
    return "tailor"  # loop back


def route_after_tailor(state: ApplicationState) -> str:
    if state.status == ApplicationStatus.FAILED:
        return "persist"
    return "validate"


# ---------------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------------


def route_after_render(state: ApplicationState) -> str:
    """Skip browser apply when dry_apply (Phase 1: render-only, no Playwright required)."""
    settings = load_settings()
    if settings["apply"].get("dry_apply", True):
        return "persist"
    if apply.get_applier(state.job.source) is None:
        return "persist"
    return "apply"


def build_graph():
    g: StateGraph = StateGraph(ApplicationState)
    g.add_node("select_candidates", node_select_candidates)
    g.add_node("tailor", node_tailor)
    g.add_node("validate", node_validate)
    g.add_node("park_for_review", node_park_for_review)
    g.add_node("render", node_render)
    g.add_node("apply", node_apply)
    g.add_node("persist", node_persist)

    g.set_entry_point("select_candidates")
    g.add_edge("select_candidates", "tailor")
    g.add_conditional_edges("tailor", route_after_tailor, {
        "validate": "validate",
        "persist": "persist",
    })
    g.add_conditional_edges("validate", route_after_validate, {
        "render": "render",
        "tailor": "tailor",
        "park_for_review": "park_for_review",
    })
    g.add_edge("park_for_review", "persist")
    g.add_conditional_edges("render", route_after_render, {
        "apply": "apply",
        "persist": "persist",
    })
    g.add_edge("apply", "persist")
    g.add_edge("persist", END)

    return g.compile()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _estimates_present(tailored) -> bool:
    return any(b.has_estimate for e in tailored.experience for b in e.bullets) or any(
        b.has_estimate for b in tailored.project_bullets
    )


def _to_markdown(state: ApplicationState) -> str:
    """Cheap markdown dump of the tailored resume for human review."""
    t = state.tailored
    if t is None:
        return ""
    lines: list[str] = []
    lines.append(f"# {t.headline}")
    lines.append("")
    if t.summary:
        lines.append("## Summary")
        lines.append(t.summary)
        lines.append("")
    if t.skills:
        lines.append("## Skills")
        lines.append(", ".join(t.skills))
        lines.append("")
    lines.append("## Experience")
    for e in t.experience:
        lines.append(f"### {e.company} — {e.title} ({e.start} – {e.end or 'Present'})")
        for b in e.bullets:
            lines.append(f"- {b.text}")
        lines.append("")
    if t.project_bullets:
        lines.append("## Selected Projects")
        for b in t.project_bullets:
            lines.append(f"- {b.text}")
        lines.append("")
    return "\n".join(lines)
