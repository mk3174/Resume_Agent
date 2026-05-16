"""Orchestrates one ingestion sweep across all configured sources."""

from __future__ import annotations

import logging
from typing import Iterable

from config_loader import load_settings
from ingest.ats_api.ashby import AshbyIngestor
from ingest.ats_api.greenhouse import GreenhouseIngestor
from ingest.ats_api.lever import LeverIngestor
from orchestrator.state import JobPosting

log = logging.getLogger(__name__)


def fetch_all() -> list[JobPosting]:
    """Fetch jobs from every configured ATS board. Errors per-board are swallowed."""
    cfg = load_settings()["ingest"]
    out: list[JobPosting] = []

    for token in cfg.get("greenhouse_boards", []) or []:
        out.extend(_safe(GreenhouseIngestor().fetch(token), token, "greenhouse"))
    for token in cfg.get("lever_boards", []) or []:
        out.extend(_safe(LeverIngestor().fetch(token), token, "lever"))
    for token in cfg.get("ashby_boards", []) or []:
        out.extend(_safe(AshbyIngestor().fetch(token), token, "ashby"))

    log.info("Ingested %d jobs across all boards", len(out))
    return out


def _safe(it: Iterable[JobPosting], token: str, source: str) -> list[JobPosting]:
    try:
        return list(it)
    except Exception as e:  # noqa: BLE001
        log.warning("ingestion error for %s/%s: %s", source, token, e)
        return []
