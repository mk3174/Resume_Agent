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
    """Fetch jobs from every configured source. Errors per-board are swallowed.

    Sources: Greenhouse / Lever / Ashby (HTTP APIs), LinkedIn / Indeed
    (Patchright scrapers, opt-in via `enabled: true` in settings).
    """
    cfg = load_settings()["ingest"]
    out: list[JobPosting] = []

    for token in cfg.get("greenhouse_boards", []) or []:
        out.extend(_safe(GreenhouseIngestor().fetch(token), token, "greenhouse"))
    for token in cfg.get("lever_boards", []) or []:
        out.extend(_safe(LeverIngestor().fetch(token), token, "lever"))
    for token in cfg.get("ashby_boards", []) or []:
        out.extend(_safe(AshbyIngestor().fetch(token), token, "ashby"))

    # Phase 4: optional Playwright-driven sources.
    li_cfg = cfg.get("linkedin", {}) or {}
    if li_cfg.get("enabled", False):
        try:
            from ingest.linkedin import LinkedInIngestor

            kws = list(li_cfg.get("search_keywords", []) or [])
            cap = int(li_cfg.get("max_searches_per_ingest") or 0)
            if cap > 0:
                kws = kws[:cap]
            for kw in kws:
                out.extend(_safe(LinkedInIngestor().fetch(kw), kw, "linkedin"))
        except Exception as e:  # noqa: BLE001
            log.warning("LinkedIn ingestor failed to load: %s", e)

    in_cfg = cfg.get("indeed", {}) or {}
    if in_cfg.get("enabled", False):
        try:
            from ingest.indeed import IndeedIngestor

            for kw in in_cfg.get("search_keywords", []) or []:
                out.extend(_safe(IndeedIngestor().fetch(kw), kw, "indeed"))
        except Exception as e:  # noqa: BLE001
            log.warning("Indeed ingestor failed to load: %s", e)

    log.info("Ingested %d jobs across all boards", len(out))
    return out


def _safe(it: Iterable[JobPosting], token: str, source: str) -> list[JobPosting]:
    try:
        return list(it)
    except Exception as e:  # noqa: BLE001
        log.warning("ingestion error for %s/%s: %s", source, token, e)
        return []
