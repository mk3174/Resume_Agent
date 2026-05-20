"""Embedding-based soft filter against the user's `interest_summary`."""

from __future__ import annotations

from functools import lru_cache

from orchestrator.state import JobPosting
from retrieval.embeddings import cosine, embed


@lru_cache(maxsize=1)
def _interest_vector(interest_text: str) -> tuple[float, ...]:
    return tuple(embed([interest_text])[0])


def _job_text(job: JobPosting) -> str:
    # Title + short excerpt is enough for filtering and keeps batch embed fast.
    return f"{job.title}\n{(job.description_text or '')[:500]}"


def score(job: JobPosting, interest_text: str) -> float:
    """Returns cosine similarity in [-1, 1]; usually [0, 1]."""
    scores = score_many([job], interest_text)
    return scores[0]


def score_many(jobs: list[JobPosting], interest_text: str) -> list[float]:
    """Batch semantic scores for many jobs (one embed round-trip per chunk)."""
    if not jobs:
        return []
    iv = list(_interest_vector(interest_text))
    texts = [_job_text(j) for j in jobs]
    vectors = embed(texts)
    return [cosine(iv, jv) for jv in vectors]
