"""The Tailor agent: JD -> tailored resume.

Two run modes (from `settings.yaml -> tailor.mode`):
  - chained_local: 3 small calls (JD extract, project rerank, STAR bullets).
                   Designed for local 8B models that handle small prompts well.
  - single_call:   one structured-output call. Use with Claude Sonnet / GPT-4o.

Both modes return the same TailoredResume + JDAnalysis. validate.py and
render/typst.py don't care which mode ran.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from config_loader import load_settings
from llm.prompts import render as render_prompt
from llm.router import get_router
from orchestrator.state import (
    JDAnalysis,
    JobPosting,
    MasterResume,
    Project,
    ResumeBullet,
    TailoredExperience,
    TailoredResume,
)

log = logging.getLogger(__name__)


def _tailor_token_limit(key: str, default: int) -> int:
    return int(load_settings()["tailor"].get(key, default))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def tailor(
    job: JobPosting,
    master: MasterResume,
    candidates: list[Project],
) -> tuple[JDAnalysis, TailoredResume]:
    settings = load_settings()
    mode = settings["tailor"]["mode"]
    top_k = int(settings["tailor"].get("top_k_projects", 7))
    candidates = candidates[:top_k]

    if mode == "single_call":
        return _single_call(job, master, candidates)
    return _chained_local(job, master, candidates)


# ---------------------------------------------------------------------------
# Mode 1: chained_local
# ---------------------------------------------------------------------------


def _chained_local(
    job: JobPosting, master: MasterResume, candidates: list[Project]
) -> tuple[JDAnalysis, TailoredResume]:
    router = get_router()

    # ---- Step 1: JD extract ----
    p = render_prompt("tailor_jd_extract.j2", job=job)
    raw, used = router.chat(
        task="tailor",
        prompt=p,
        json_mode=True,
        max_tokens=800,
        temperature=0.1,
        think=False,
    )
    log.info("JD extract via %s", used)
    jd = JDAnalysis(**_parse_json(raw))

    # ---- Step 2: rerank ----
    if not candidates:
        selected: list[Project] = []
    else:
        p = render_prompt("tailor_project_rerank.j2", job=job, jd=jd, candidates=candidates)
        raw, used = router.chat(
            task="rerank",
            prompt=p,
            json_mode=True,
            max_tokens=512,
            temperature=0.1,
            think=False,
        )
        log.info("rerank via %s", used)
        try:
            sel = _parse_json(raw)
        except json.JSONDecodeError:
            log.warning(
                "rerank JSON parse failed (%s); using top embedding candidates",
                used,
            )
            sel = {}
        ids: list[str] = sel.get("selected_project_ids") or []
        by_id = {c.id: c for c in candidates}
        selected = [by_id[i] for i in ids if i in by_id][:4]
        if not selected:  # graceful fallback
            selected = candidates[:3]

    # ---- Step 3: STAR bullets (large JSON; truncation → Unterminated string) ----
    p = render_prompt(
        "tailor_star_bullets.j2",
        job=job,
        jd=jd,
        master=master,
        selected_projects=selected,
    )
    body, used = _chat_json(
        router,
        task="tailor",
        prompt=p,
        max_tokens=_tailor_token_limit("star_bullets_max_tokens", 8192),
        temperature=0.3,
        think=False,
        step_label="STAR bullets",
    )
    log.info("STAR bullets via %s", used)

    tailored = _build_tailored(body, master, selected_ids=[p.id for p in selected])
    return jd, tailored


# ---------------------------------------------------------------------------
# Mode 2: single_call
# ---------------------------------------------------------------------------


def _single_call(
    job: JobPosting, master: MasterResume, candidates: list[Project]
) -> tuple[JDAnalysis, TailoredResume]:
    router = get_router()
    p = render_prompt("tailor_single_call.j2", job=job, master=master, candidates=candidates)
    body, used = _chat_json(
        router,
        task="tailor",
        prompt=p,
        max_tokens=_tailor_token_limit("single_call_max_tokens", 8000),
        temperature=0.2,
        think=False,
        step_label="single-call tailor",
    )
    log.info("single-call tailor via %s", used)

    jd_raw = body.get("jd_analysis") or {}
    jd = JDAnalysis(
        must_have_keywords=jd_raw.get("must_have_keywords") or [],
        nice_to_have_keywords=jd_raw.get("nice_to_have_keywords") or [],
        semantic_themes=jd_raw.get("semantic_themes") or [],
        seniority=jd_raw.get("seniority"),
        primary_responsibilities=jd_raw.get("primary_responsibilities") or [],
    )
    tailored = _build_tailored(body, master, selected_ids=body.get("selected_project_ids") or [])
    return jd, tailored


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chat_json(
    router,
    *,
    task: str,
    prompt: str,
    max_tokens: int,
    temperature: float = 0.2,
    think: bool | None = False,
    step_label: str = "tailor",
) -> tuple[dict[str, Any], str]:
    """Call LLM with json_mode and parse; retry with 2x tokens if output was truncated."""
    mt = max_tokens
    last_err: json.JSONDecodeError | None = None
    last_raw = ""
    used = ""
    for attempt in range(3):
        raw, used = router.chat(
            task=task,
            prompt=prompt,
            json_mode=True,
            max_tokens=mt,
            temperature=temperature,
            think=think,
        )
        last_raw = raw
        try:
            return _parse_json(raw), used
        except json.JSONDecodeError as e:
            last_err = e
            if attempt >= 2:
                break
            next_mt = min(mt * 2, 16384)
            log.warning(
                "%s JSON truncated or invalid (%s, %d chars); retrying with max_tokens=%s",
                step_label,
                e,
                len(raw or ""),
                next_mt,
            )
            mt = next_mt
    log.error("JSON parse failed after retries: %s\nraw_tail=%s", last_err, (last_raw or "")[-500:])
    raise last_err  # type: ignore[misc]


def _parse_json(raw: str) -> dict[str, Any]:
    """Tolerant JSON parser: strips code fences, trims junk, recovers if possible."""
    txt = raw.strip()
    if not txt:
        raise json.JSONDecodeError("empty model response", "", 0)
    if txt.startswith("```"):
        # strip triple-backtick fences with optional ```json
        txt = re.sub(r"^```[a-zA-Z]*\n", "", txt)
        txt = re.sub(r"\n```\s*$", "", txt)
    # If the model added prose before/after, find the outermost {...}
    if not txt.startswith("{"):
        m = re.search(r"\{[\s\S]*\}\s*$", txt)
        if m:
            txt = m.group(0)
    try:
        return json.loads(txt)
    except json.JSONDecodeError as e:
        if "Unterminated string" in str(e) or not txt.rstrip().endswith("}"):
            log.error(
                "JSON parse failed (likely max_tokens truncation, len=%d): %s",
                len(txt),
                e,
            )
        else:
            log.error("JSON parse failed: %s\nraw=%s", e, raw[:1000])
        raise


def _build_tailored(
    body: dict[str, Any], master: MasterResume, *, selected_ids: list[str]
) -> TailoredResume:
    """Map a raw model response (either mode) into a TailoredResume.

    We trust the model's experience timeline ONLY for company/title/dates that
    match the master. Anything else is overridden with master's truth.
    """
    master_by_company_start = {(e.company.lower(), e.start): e for e in master.experience}

    experience: list[TailoredExperience] = []
    for raw_exp in body.get("experience") or []:
        company = (raw_exp.get("company") or "").strip()
        start = (raw_exp.get("start") or "").strip()
        key = (company.lower(), start)
        if key not in master_by_company_start:
            log.warning(
                "Tailor returned experience entry not in master: %s @ %s; dropping",
                company,
                start,
            )
            continue
        truth = master_by_company_start[key]
        bullets: list[ResumeBullet] = []
        for b in raw_exp.get("bullets") or []:
            bullets.append(_safe_bullet(b, default_source="experience", default_ref=f"experience:{truth.company}:{truth.start}"))
        experience.append(
            TailoredExperience(
                company=truth.company,
                title=truth.title,
                start=truth.start,
                end=truth.end,
                bullets=bullets,
            )
        )

    project_bullets = [
        _safe_bullet(b, default_source="project", default_ref="project:unknown")
        for b in body.get("project_bullets") or []
    ]

    return TailoredResume(
        headline=(body.get("headline") or master.headline).strip() or master.headline,
        summary=(body.get("summary") or master.summary).strip(),
        skills=list(body.get("skills") or master.skills),
        experience=experience,
        selected_projects=list(selected_ids),
        project_bullets=project_bullets,
        education=master.education,
        cover_paragraph=(body.get("cover_paragraph") or "").strip(),
    )


def _safe_bullet(b: dict[str, Any], *, default_source: str, default_ref: str) -> ResumeBullet:
    """Coerce a raw bullet dict into a ResumeBullet, with safe defaults."""
    text = (b.get("text") or "").strip()
    src = b.get("source") or default_source
    if src not in ("project", "experience", "estimate", "summary"):
        src = default_source
    has_est = bool(b.get("has_estimate"))
    if "[ESTIMATE]" in text and not has_est:
        has_est = True
    return ResumeBullet(
        text=text,
        source=src,  # type: ignore[arg-type]
        source_ref=(b.get("source_ref") or default_ref).strip(),
        star=b.get("star") or {},
        keywords_hit=list(b.get("keywords_hit") or []),
        has_estimate=has_est,
    )
