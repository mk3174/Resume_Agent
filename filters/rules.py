"""Hard rule filters from preferences.yaml. Returns True if the job should be kept."""

from __future__ import annotations

import re
from typing import Any

from orchestrator.state import JobPosting

# ATS boards use inconsistent employment labels.
_EMPLOYMENT_ALIASES: dict[str, str] = {
    "full-time": "full-time",
    "parttime": "part-time",
    "part time": "part-time",
    "part-time": "part-time",
    "contract": "contract",
    "intern": "intern",
    "internship": "intern",
}

# Title/description patterns that imply >4 years required (checked on combined blob).
_YEARS_PLUS_RE = re.compile(r"(\d+)\s*\+\s*years", re.I)
_YEARS_RANGE_RE = re.compile(r"(\d+)\s*[-–]\s*(\d+)\s*years", re.I)
_YEARS_MIN_RE = re.compile(r"(?:minimum|min\.?|at least)\s*(\d+)\s*years", re.I)
_YEARS_OF_EXP_RE = re.compile(
    r"(\d+)\s*years\s+(?:of\s+)?(?:professional\s+)?experience", re.I
)


def passes_hard_filters(job: JobPosting, prefs: dict[str, Any]) -> tuple[bool, str | None]:
    """Returns (kept, reason_if_dropped)."""
    hf = prefs.get("hard_filters", {}) or {}

    # location
    loc_cfg = hf.get("locations") or {}
    deny = [s.lower() for s in (loc_cfg.get("deny") or [])]
    allow = [s.lower() for s in (loc_cfg.get("allow") or [])]
    job_loc = (job.location or "").lower()
    if deny and any(d in job_loc for d in deny):
        return False, f"deny location: {job.location}"
    if allow and not any(a in job_loc for a in allow) and job_loc:
        return False, f"location not in allow list: {job.location}"

    # employment type
    et_allowed = hf.get("employment_types") or []
    if et_allowed and job.employment_type:
        norm = _normalize_employment(job.employment_type)
        allowed_norm = {_normalize_employment(e) for e in et_allowed}
        if norm not in allowed_norm:
            return False, f"employment_type {job.employment_type} not allowed"

    title_lower = (job.title or "").lower()
    desc_blob = f"{job.title}\n{job.description_text}".lower()

    # seniority: deny substrings in title
    sen_cfg = hf.get("seniority", {}) or {}
    sen_deny = [s.lower() for s in sen_cfg.get("deny") or []]
    for s in sen_deny:
        if s and s in title_lower:
            return False, f"title contains denied seniority '{s}'"

    # seniority: deny substrings in title + description (manager, 10+ years, etc.)
    sen_deny_text = [s.lower() for s in sen_cfg.get("deny_in_text") or []]
    for s in sen_deny_text:
        if s and s in desc_blob:
            return False, f"posting contains denied phrase '{s}'"

    # seniority: optional title allow-list (at least one must match)
    sen_allow = [s.lower() for s in sen_cfg.get("allow_title_any") or []]
    if sen_allow and not any(s in title_lower for s in sen_allow):
        return False, "title does not match any allowed seniority keyword"

    # max years of experience required in posting (drop 5+, "minimum 6 years", etc.)
    max_years = sen_cfg.get("max_experience_years")
    if max_years is not None:
        over, detail = _exceeds_max_experience(desc_blob, int(max_years))
        if over:
            return False, detail

    # excluded companies
    if (job.company or "").lower() in [c.lower() for c in (hf.get("exclude_companies") or [])]:
        return False, "company on exclude list"

    # salary
    min_salary = hf.get("min_salary_usd") or 0
    if min_salary and job.salary_max_usd is not None and job.salary_max_usd < min_salary:
        return False, f"salary_max {job.salary_max_usd} < min {min_salary}"

    # required keywords (any-of)
    req_any = [k.lower() for k in (hf.get("required_keywords_any") or [])]
    if req_any and not any(k in desc_blob for k in req_any):
        return False, "no required keyword present"

    excluded = [k.lower() for k in (hf.get("excluded_keywords") or [])]
    for k in excluded:
        if k in desc_blob:
            return False, f"contains excluded keyword '{k}'"

    return True, None


def _normalize_employment(value: str) -> str:
    key = (value or "").strip().lower()
    return _EMPLOYMENT_ALIASES.get(key, key)


def _exceeds_max_experience(blob: str, max_years: int) -> tuple[bool, str | None]:
    """True if the posting text implies more than ``max_years`` experience required."""
    for m in _YEARS_PLUS_RE.finditer(blob):
        if int(m.group(1)) > max_years:
            return True, f"requires {m.group(1)}+ years experience"
    for m in _YEARS_RANGE_RE.finditer(blob):
        low = int(m.group(1))
        if low > max_years:
            return True, f"requires {low}-{m.group(2)} years experience"
    for m in _YEARS_MIN_RE.finditer(blob):
        if int(m.group(1)) > max_years:
            return True, f"minimum {m.group(1)} years experience"
    for m in _YEARS_OF_EXP_RE.finditer(blob):
        if int(m.group(1)) > max_years:
            return True, f"requires {m.group(1)} years experience"
    return False, None
