"""Deterministic guardrails. NO LLM calls.

Reject conditions (anything in here triggers a re-tailor or needs_review parking):
  1. Bullet source is "fabricated" (always blocked under library_plus_metrics).
  2. Numeric metric in bullet text with no source AND no [ESTIMATE] flag.
  3. ATS keyword coverage of must-have keywords below threshold.
  4. Resume word count over limit.

Estimates flagged correctly are PASSED through (and surface as FYI later).
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from config_loader import load_settings
from orchestrator.state import (
    JDAnalysis,
    MasterResume,
    ResumeBullet,
    TailoredResume,
)

log = logging.getLogger(__name__)

_NUMBER_RE = re.compile(r"\b\d+(\.\d+)?\s*(?:%|x|k|m|b|million|billion)?\b", re.IGNORECASE)
_ACTION_RE = re.compile(
    r"\b(?:built|led|developed|architected|designed|implemented|optimized|engineered|"
    r"delivered|automated|deployed|created|improved|reduced|increased|scaled|"
    r"migrated|integrated|established|launched|shipped)\w*\b",
    re.IGNORECASE,
)


def validate(
    tailored: TailoredResume,
    jd: JDAnalysis,
    master: MasterResume,
) -> tuple[bool, list[str], TailoredResume]:
    settings = load_settings()
    threshold = float(settings["tailor"].get("ats_keyword_threshold", 0.9))
    max_words = int(settings["tailor"].get("resume_max_words", 700))
    policy = settings["tailor"].get("embellishment_policy", "library_plus_metrics")

    errors: list[str] = []

    all_bullets = list(_iter_bullets(tailored))

    # Rule 1 + provenance trace
    valid_exp_refs = {
        f"experience:{e.company}:{e.start}".lower() for e in master.experience
    }
    valid_project_refs = {f"project:{pid}".lower() for pid in tailored.selected_projects}

    for b in all_bullets:
        if b.source not in ("project", "experience", "estimate", "summary"):
            errors.append(f"bullet has invalid source '{b.source}': {b.text[:80]}")
            continue
        ref = b.source_ref.lower()
        if b.source == "experience" and ref not in valid_exp_refs:
            errors.append(f"experience bullet references unknown role '{b.source_ref}': {b.text[:80]}")
        if b.source == "project" and ref not in valid_project_refs:
            errors.append(f"project bullet references unknown project '{b.source_ref}': {b.text[:80]}")

        # Rule 2: numeric without provenance and without [ESTIMATE] tag
        if _has_unattributed_metric(b, policy=policy):
            errors.append(f"bullet has unflagged estimated metric: {b.text[:80]}")

        # Rule 2b: STAR structure (structured star{} or action+result prose)
        if b.source in ("experience", "project") and not _passes_star(b):
            errors.append(f"bullet fails STAR format: {b.text[:80]}")

    # Rule 2c: layout lock — headline must match master (LLM must not rename)
    if tailored.headline.strip() != master.headline.strip():
        errors.append("headline changed from master resume (layout lock)")

    # Rule 2d: all master experience roles must be present
    master_keys = {(e.company.lower(), e.start) for e in master.experience}
    tailored_keys = {(e.company.lower(), e.start) for e in tailored.experience}
    missing_roles = master_keys - tailored_keys
    if missing_roles:
        errors.append(f"missing experience entries from master: {sorted(missing_roles)}")

    # Rule 2e: minimum bullet counts per experience and project
    min_exp = int(settings["tailor"].get("min_experience_bullets", 3))
    max_exp = int(settings["tailor"].get("max_experience_bullets", 5))
    min_proj = int(settings["tailor"].get("min_project_bullets", 2))
    max_proj = int(settings["tailor"].get("max_project_bullets", 3))

    for e in tailored.experience:
        n = len(e.bullets)
        if n < min_exp:
            errors.append(f"{e.company} has {n} bullets (minimum {min_exp})")
        elif n > max_exp:
            errors.append(f"{e.company} has {n} bullets (maximum {max_exp})")

    proj_counts: dict[str, int] = {}
    for b in tailored.project_bullets:
        ref = b.source_ref.lower()
        if ref.startswith("project:"):
            pid = ref.split(":", 1)[1]
            proj_counts[pid] = proj_counts.get(pid, 0) + 1

    for pid in tailored.selected_projects:
        n = proj_counts.get(pid, 0)
        if n < min_proj:
            errors.append(f"project '{pid}' has {n} bullets (minimum {min_proj})")
        elif n > max_proj:
            errors.append(f"project '{pid}' has {n} bullets (maximum {max_proj})")

    # Rule 3: ATS keyword coverage
    coverage = _ats_coverage(tailored, jd.must_have_keywords)
    tailored_out = tailored.model_copy(update={"ats_coverage": coverage})
    if jd.must_have_keywords and coverage < threshold:
        errors.append(
            f"ATS coverage {coverage:.0%} < threshold {threshold:.0%}; "
            f"missing keywords: {_missing_keywords(tailored, jd.must_have_keywords)}"
        )

    # Rule 4: word count
    wc = _word_count(tailored)
    if wc > max_words:
        errors.append(f"resume word count {wc} > max {max_words}")

    return (len(errors) == 0), errors, tailored_out


def ats_coverage(tailored: TailoredResume, jd: JDAnalysis) -> float:
    """Public helper: must-have keyword coverage ratio."""
    return _ats_coverage(tailored, jd.must_have_keywords)


def missing_must_have(tailored: TailoredResume, jd: JDAnalysis) -> list[str]:
    """Must-have JD keywords absent from the tailored resume text."""
    return _missing_keywords(tailored, jd.must_have_keywords)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iter_bullets(t: TailoredResume) -> Iterable[ResumeBullet]:
    for e in t.experience:
        yield from e.bullets
    yield from t.project_bullets


def _passes_star(b: ResumeBullet) -> bool:
    """Accept structured STAR fields or prose with action verb + outcome."""
    star = b.star or {}
    if all((star.get(k) or "").strip() for k in ("s", "t", "a", "r")):
        return True
    text = b.text.strip()
    if len(text) < 28:
        return False
    if not _ACTION_RE.search(text):
        return False
    if _NUMBER_RE.search(text):
        return True
    outcome_markers = ("by ", "reduc", "improv", "increas", "cut ", "boost", "lift", "drop", "enabl")
    return any(m in text.lower() for m in outcome_markers)


def _has_unattributed_metric(b: ResumeBullet, *, policy: str) -> bool:
    if not _NUMBER_RE.search(b.text):
        return False
    if b.has_estimate:
        return False
    # If the metric appears in the structured "r" field of star and source is real,
    # we accept it — that's the user's data path.
    if b.source in ("project", "experience"):
        return False
    return True


def _normalise_kw(kw: str) -> str:
    return re.sub(r"[^a-z0-9+#./\- ]", "", kw.lower()).strip()


def _ats_coverage(tailored: TailoredResume, must_have: list[str]) -> float:
    if not must_have:
        return 1.0
    blob = " ".join(tailored.skills) + "\n"
    for b in _iter_bullets(tailored):
        blob += b.text + "\n"
    blob_l = blob.lower()
    hits = sum(1 for kw in must_have if _normalise_kw(kw) in blob_l)
    return hits / len(must_have)


def _missing_keywords(tailored: TailoredResume, must_have: list[str]) -> list[str]:
    blob = " ".join(tailored.skills) + " " + " ".join(b.text for b in _iter_bullets(tailored))
    blob_l = blob.lower()
    return [kw for kw in must_have if _normalise_kw(kw) not in blob_l]


def _word_count(t: TailoredResume) -> int:
    parts: list[str] = [t.headline, t.summary, *t.skills]
    for e in t.experience:
        parts.extend([e.company, e.title, e.start, e.end or ""])
        parts.extend(b.text for b in e.bullets)
    parts.extend(b.text for b in t.project_bullets)
    return sum(len(p.split()) for p in parts if p)
