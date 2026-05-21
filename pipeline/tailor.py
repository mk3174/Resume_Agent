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
from pipeline import validate as validate_mod

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
        jd, tailored = _single_call(job, master, candidates)
    else:
        jd, tailored = _chained_local(job, master, candidates)
    tailored = tune_ats_coverage(tailored, jd, master, job)
    return jd, tailored


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
        task="tailor_jd",
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
        task="tailor_star",
        prompt=p,
        max_tokens=_tailor_token_limit("star_bullets_max_tokens", 8192),
        temperature=0.3,
        think=False,
        step_label="STAR bullets",
    )
    log.info("STAR bullets via %s", used)

    tailored = _build_tailored(body, master, selected_ids=[p.id for p in selected])
    return jd, tailored


def tune_ats_coverage(
    tailored: TailoredResume,
    jd: JDAnalysis,
    master: MasterResume,
    job: JobPosting,
) -> TailoredResume:
    """Raise must-have keyword coverage toward settings.tailor.ats_keyword_threshold (default 0.9).

    Pass 1: append missing keywords to skills (deterministic).
    Pass 2+: local Ollama (`task=tailor_ats`) rewrites summary/skills/bullets.
    """
    settings = load_settings()["tailor"]
    threshold = float(settings.get("ats_keyword_threshold", 0.9))
    max_attempts = int(settings.get("ats_tune_max_attempts", 3))

    if not jd.must_have_keywords:
        cov = validate_mod.ats_coverage(tailored, jd)
        return tailored.model_copy(update={"ats_coverage": cov})

    current = tailored
    for attempt in range(max_attempts):
        cov = validate_mod.ats_coverage(current, jd)
        current = current.model_copy(update={"ats_coverage": cov})
        if cov >= threshold:
            log.info("ATS coverage %.0f%% >= threshold %.0f%%", cov * 100, threshold * 100)
            return current

        missing = validate_mod.missing_must_have(current, jd)
        if not missing:
            return current

        log.info(
            "ATS tune attempt %d: %.0f%% coverage; missing %s",
            attempt + 1,
            cov * 100,
            missing[:8],
        )
        if attempt == 0:
            current = _deterministic_skills_boost(current, missing)
            continue

        try:
            current = _ollama_ats_boost(current, jd, master, job, missing)
        except Exception as e:  # noqa: BLE001
            log.warning("Ollama ATS boost failed: %s; using skills-only fallback", e)
            current = _deterministic_skills_boost(current, missing)

    cov = validate_mod.ats_coverage(current, jd)
    return current.model_copy(update={"ats_coverage": cov})


def _deterministic_skills_boost(tailored: TailoredResume, missing: list[str]) -> TailoredResume:
    skills = list(tailored.skills)
    seen = {s.lower() for s in skills}
    for kw in missing:
        if kw.lower() in seen:
            continue
        skills.append(kw)
        seen.add(kw.lower())
        if len(skills) >= 18:
            break
    summary = tailored.summary
    if missing and missing[0].lower() not in summary.lower():
        summary = f"{summary.rstrip()} Relevant strengths include {', '.join(missing[:4])}."
    return tailored.model_copy(update={"skills": skills, "summary": summary.strip()})


def _ollama_ats_boost(
    tailored: TailoredResume,
    jd: JDAnalysis,
    master: MasterResume,
    job: JobPosting,
    missing: list[str],
) -> TailoredResume:
    router = get_router()
    payload = {
        "summary": tailored.summary,
        "skills": tailored.skills,
        "experience": [
            {
                "company": e.company,
                "title": e.title,
                "start": e.start,
                "end": e.end,
                "bullets": [b.model_dump() for b in e.bullets],
            }
            for e in tailored.experience
        ],
        "project_bullets": [b.model_dump() for b in tailored.project_bullets],
    }
    p = render_prompt(
        "tailor_ats_boost.j2",
        missing_keywords=missing,
        current_json=json.dumps(payload, indent=2),
    )
    body, used = _chat_json(
        router,
        task="tailor_ats",
        prompt=p,
        max_tokens=4096,
        temperature=0.2,
        think=False,
        step_label="ATS boost",
    )
    log.info("ATS boost via %s", used)
    merged = _build_tailored(body, master, selected_ids=list(tailored.selected_projects))
    return merged.model_copy(
        update={
            "headline": master.headline,
            "education": list(master.education),
            "certifications": list(master.certifications),
            "publications": list(master.publications),
        }
    )


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


def _bullet_limits() -> tuple[int, int, int, int]:
    """(min_exp, max_exp, min_proj, max_proj) from settings."""
    settings = load_settings()["tailor"]
    return (
        int(settings.get("min_experience_bullets", 3)),
        int(settings.get("max_experience_bullets", 5)),
        int(settings.get("min_project_bullets", 2)),
        int(settings.get("max_project_bullets", 3)),
    )


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _base_bullet(text: str, *, source: str, source_ref: str) -> ResumeBullet:
    return ResumeBullet(text=text.strip(), source=source, source_ref=source_ref)  # type: ignore[arg-type]


def _pad_bullet_list(
    bullets: list[ResumeBullet],
    fallbacks: list[str],
    *,
    source: str,
    source_ref: str,
    min_count: int,
    max_count: int,
) -> list[ResumeBullet]:
    """Keep LLM bullets first; pad from fallbacks until min_count; trim to max_count."""
    out = [b for b in bullets if (b.text or "").strip()]
    seen = {_norm_text(b.text) for b in out}
    for text in fallbacks:
        if len(out) >= min_count:
            break
        t = text.strip()
        if not t or _norm_text(t) in seen:
            continue
        out.append(_base_bullet(t, source=source, source_ref=source_ref))
        seen.add(_norm_text(t))
    return out[:max_count]


def _master_project_base_bullets(master: MasterResume, project_id: str) -> list[str]:
    """Master ### Personal Projects bullets matched to a project library id by title."""
    from retrieval.project_library import load_projects_from_disk

    proj = next((p for p in load_projects_from_disk() if p.id == project_id), None)
    if not proj:
        return []
    title_key = proj.title.lower()
    for entries in (master.projects_sections or {}).values():
        for entry in entries:
            entry_title = (entry.get("title") or "").lower()
            if entry_title == title_key or title_key in entry_title or entry_title in title_key:
                return list(entry.get("base_bullets") or [])
    return []


def _project_fallback_bullets(master: MasterResume, project_id: str) -> list[str]:
    """Padding sources for a project: master outline, then library metrics/body."""
    from retrieval.project_library import load_projects_from_disk

    fallbacks = list(_master_project_base_bullets(master, project_id))
    proj = next((p for p in load_projects_from_disk() if p.id == project_id), None)
    if not proj:
        return fallbacks
    fallbacks.extend(proj.impact_metrics or [])
    if proj.summary:
        fallbacks.append(proj.summary.strip())
    for chunk in re.split(r"(?<=[.!?])\s+", (proj.body or "").strip()):
        chunk = chunk.strip()
        if len(chunk) > 40:
            fallbacks.append(chunk)
    return fallbacks


def _merge_project_bullets(
    body: dict[str, Any],
    master: MasterResume,
    *,
    selected_ids: list[str],
) -> list[ResumeBullet]:
    """Map LLM project bullets and pad each selected id to min/max counts."""
    min_exp, max_exp, min_proj, max_proj = _bullet_limits()
    del min_exp, max_exp  # experience handled separately

    by_id: dict[str, list[ResumeBullet]] = {pid: [] for pid in selected_ids}
    orphan: list[ResumeBullet] = []

    for b in body.get("project_bullets") or []:
        ref = (b.get("source_ref") or "").strip()
        if ref.startswith("project:"):
            pid = ref.split(":", 1)[1]
        elif selected_ids:
            pid = selected_ids[min(len(orphan), len(selected_ids) - 1)]
            ref = f"project:{pid}"
        else:
            continue
        bullet = _safe_bullet(b, default_source="project", default_ref=ref)
        if pid in by_id:
            by_id[pid].append(bullet)
        else:
            orphan.append(bullet)

    for i, extra in enumerate(orphan):
        if not selected_ids:
            break
        pid = selected_ids[i % len(selected_ids)]
        by_id.setdefault(pid, []).append(extra)

    merged: list[ResumeBullet] = []
    for pid in selected_ids:
        padded = _pad_bullet_list(
            by_id.get(pid, []),
            _project_fallback_bullets(master, pid),
            source="project",
            source_ref=f"project:{pid}",
            min_count=min_proj,
            max_count=max_proj,
        )
        merged.extend(padded)
    return merged


def _build_tailored(
    body: dict[str, Any], master: MasterResume, *, selected_ids: list[str]
) -> TailoredResume:
    """Map LLM JSON into TailoredResume.

    Layout-locked from master: headline, education, certifications, publications,
    experience companies/titles/dates/locations. LLM may only change summary,
    skills, experience bullet text, and project bullets.
    """
    experience = _merge_experience(body, master)
    project_bullets = _merge_project_bullets(body, master, selected_ids=list(selected_ids))

    skills_raw = body.get("skills") or master.skills
    skills = [str(s).strip() for s in skills_raw if str(s).strip()]

    return TailoredResume(
        headline=master.headline,
        summary=(body.get("summary") or master.summary).strip(),
        skills=skills or list(master.skills),
        experience=experience,
        selected_projects=list(selected_ids),
        project_bullets=project_bullets,
        education=list(master.education),
        certifications=list(master.certifications),
        publications=list(master.publications),
        cover_paragraph=(body.get("cover_paragraph") or "").strip(),
    )


def _merge_experience(body: dict[str, Any], master: MasterResume) -> list[TailoredExperience]:
    """Keep every master role; LLM supplies bullets or we fall back to base_bullets."""
    min_exp, max_exp, _, _ = _bullet_limits()
    master_by_key = {(e.company.lower(), e.start): e for e in master.experience}
    llm_bullets: dict[tuple[str, str], list[ResumeBullet]] = {}

    for raw_exp in body.get("experience") or []:
        company = (raw_exp.get("company") or "").strip()
        start = (raw_exp.get("start") or "").strip()
        key = (company.lower(), start)
        if key not in master_by_key:
            log.warning("Tailor returned experience entry not in master: %s @ %s; dropping", company, start)
            continue
        truth = master_by_key[key]
        bullets = [
            _safe_bullet(b, default_source="experience", default_ref=f"experience:{truth.company}:{truth.start}")
            for b in raw_exp.get("bullets") or []
        ]
        if bullets:
            llm_bullets[key] = bullets

    experience: list[TailoredExperience] = []
    for truth in master.experience:
        key = (truth.company.lower(), truth.start)
        source_ref = f"experience:{truth.company}:{truth.start}"
        bullets = llm_bullets.get(key)
        if not bullets:
            bullets = [
                _base_bullet(b, source="experience", source_ref=source_ref)
                for b in truth.base_bullets
            ]
        bullets = _pad_bullet_list(
            bullets,
            truth.base_bullets,
            source="experience",
            source_ref=source_ref,
            min_count=min_exp,
            max_count=max_exp,
        )
        experience.append(
            TailoredExperience(
                company=truth.company,
                title=truth.title,
                start=truth.start,
                end=truth.end,
                location=truth.location,
                bullets=bullets,
            )
        )
    return experience


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
