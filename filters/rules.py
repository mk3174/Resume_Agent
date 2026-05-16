"""Hard rule filters from preferences.yaml. Returns True if the job should be kept."""

from __future__ import annotations

from typing import Any

from orchestrator.state import JobPosting


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
    if et_allowed and job.employment_type and job.employment_type not in et_allowed:
        return False, f"employment_type {job.employment_type} not allowed"

    # seniority deny (substring on title)
    title_lower = (job.title or "").lower()
    sen_deny = [s.lower() for s in (hf.get("seniority", {}) or {}).get("deny") or []]
    for s in sen_deny:
        if s and s in title_lower:
            return False, f"title contains denied seniority '{s}'"

    # excluded companies
    if (job.company or "").lower() in [c.lower() for c in (hf.get("exclude_companies") or [])]:
        return False, "company on exclude list"

    # salary
    min_salary = hf.get("min_salary_usd") or 0
    if min_salary and job.salary_max_usd is not None and job.salary_max_usd < min_salary:
        return False, f"salary_max {job.salary_max_usd} < min {min_salary}"

    # required keywords (any-of)
    desc_blob = f"{job.title}\n{job.description_text}".lower()
    req_any = [k.lower() for k in (hf.get("required_keywords_any") or [])]
    if req_any and not any(k in desc_blob for k in req_any):
        return False, "no required keyword present"

    excluded = [k.lower() for k in (hf.get("excluded_keywords") or [])]
    for k in excluded:
        if k in desc_blob:
            return False, f"contains excluded keyword '{k}'"

    return True, None
