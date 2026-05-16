"""Filter orchestration: hard rules first, then soft semantic score."""

from __future__ import annotations

import logging

from config_loader import load_preferences
from filters.rules import passes_hard_filters
from filters.semantic import score
from orchestrator.state import JobPosting

log = logging.getLogger(__name__)


def filter_jobs(jobs: list[JobPosting]) -> list[tuple[JobPosting, float]]:
    """Returns kept jobs with their semantic score, sorted high-to-low."""
    prefs = load_preferences()
    interest = prefs.get("interest_summary", "")
    sem_min = prefs.get("semantic_score_min", 0.0)

    kept: list[tuple[JobPosting, float]] = []
    for job in jobs:
        ok, reason = passes_hard_filters(job, prefs)
        if not ok:
            log.debug("dropped %s: %s", job.stable_key, reason)
            continue
        try:
            s = score(job, interest) if interest else 1.0
        except Exception as e:  # noqa: BLE001
            log.warning("semantic score failed for %s: %s", job.stable_key, e)
            s = 1.0  # don't punish for embedding errors
        if s < sem_min:
            log.debug("dropped %s: low semantic %.2f < %.2f", job.stable_key, s, sem_min)
            continue
        kept.append((job, s))

    kept.sort(key=lambda t: t[1], reverse=True)
    log.info("Kept %d/%d jobs after filtering", len(kept), len(jobs))
    return kept
