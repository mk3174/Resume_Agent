"""Embedding-based soft filter against the user's `interest_summary`."""

from __future__ import annotations

from functools import lru_cache

from orchestrator.state import JobPosting
from retrieval.embeddings import cosine, embed


@lru_cache(maxsize=1)
def _interest_vector(interest_text: str) -> tuple[float, ...]:
    return tuple(embed([interest_text])[0])


def score(job: JobPosting, interest_text: str) -> float:
    """Returns cosine similarity in [-1, 1]; usually [0, 1]."""
    iv = list(_interest_vector(interest_text))
    jv = embed([f"{job.title}\n{job.description_text[:2000]}"])[0]
    return cosine(iv, jv)
