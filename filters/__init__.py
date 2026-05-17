"""Filter orchestration: hard rules first, then soft semantic score."""

from __future__ import annotations

import logging

from config_loader import load_preferences
from filters.rules import passes_hard_filters
from filters.semantic import score_many
from orchestrator.state import JobPosting

log = logging.getLogger(__name__)


def filter_jobs(jobs: list[JobPosting]) -> list[tuple[JobPosting, float]]:
    """Returns kept jobs with their semantic score, sorted high-to-low."""
    prefs = load_preferences()
    interest = prefs.get("interest_summary", "")
    sem_min = prefs.get("semantic_score_min", 0.0)

    survivors: list[JobPosting] = []
    for job in jobs:
        ok, reason = passes_hard_filters(job, prefs)
        if not ok:
            log.debug("dropped %s: %s", job.stable_key, reason)
            continue
        survivors.append(job)

    kept: list[tuple[JobPosting, float]] = []
    if not interest:
        kept = [(j, 1.0) for j in survivors]
    else:
        try:
            scores = score_many(survivors, interest)
            for job, s in zip(survivors, scores, strict=True):
                if s < sem_min:
                    log.debug("dropped %s: low semantic %.2f < %.2f", job.stable_key, s, sem_min)
                    continue
                kept.append((job, s))
        except Exception as e:  # noqa: BLE001
            log.warning("batch semantic scoring failed, keeping survivors at score=1.0: %s", e)
            kept = [(j, 1.0) for j in survivors]

    kept.sort(key=lambda t: t[1], reverse=True)
    log.info("Kept %d/%d jobs after filtering", len(kept), len(jobs))
    return kept
