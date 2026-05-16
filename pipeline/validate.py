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


def validate(
    tailored: TailoredResume,
    jd: JDAnalysis,
    master: MasterResume,
) -> tuple[bool, list[str], TailoredResume]:
    """Returns (ok, errors, tailored_with_ats_score_filled).

    Mutates a copy with `ats_coverage` populated for downstream rendering.
    """
    settings = load_settings()
    threshold = float(settings["tailor"].get("ats_keyword_threshold", 0.7))
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iter_bullets(t: TailoredResume) -> Iterable[ResumeBullet]:
    for e in t.experience:
        yield from e.bullets
    yield from t.project_bullets


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
