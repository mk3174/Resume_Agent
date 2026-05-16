"""Greenhouse public Job Board API.

Endpoint: https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
No auth, no rate limits worth worrying about for our volume.
"""

from __future__ import annotations

import logging
from typing import Iterable

import httpx

from orchestrator.state import JobPosting, JobSource

log = logging.getLogger(__name__)

BASE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
TIMEOUT = httpx.Timeout(20.0)


class GreenhouseIngestor:
    source_name = "greenhouse"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "resume-agent/0.1"})

    def fetch(self, board_token: str) -> Iterable[JobPosting]:
        url = BASE.format(token=board_token) + "?content=true"
        try:
            r = self._client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("greenhouse fetch failed for %s: %s", board_token, e)
            return []

        data = r.json()
        for raw in data.get("jobs", []):
            yield self._parse(raw, board_token)

    @staticmethod
    def _parse(raw: dict, company: str) -> JobPosting:
        offices = raw.get("offices") or []
        location = (
            raw.get("location", {}).get("name")
            or (offices[0].get("name") if offices else None)
        )
        # Greenhouse content is HTML. Convert to text for the LLM.
        html = raw.get("content", "")
        text = _html_to_text(html)
        dept = (raw.get("departments") or [{}])[0].get("name")
        return JobPosting(
            source=JobSource.GREENHOUSE,
            external_id=str(raw["id"]),
            company=raw.get("company_name") or company.title(),
            title=raw["title"],
            location=location,
            department=dept,
            description_text=text,
            description_html=html,
            apply_url=raw.get("absolute_url", ""),
            raw=raw,
        )


def _html_to_text(html: str) -> str:
    """Cheap HTML->text. Good enough for ATS pages which are mostly <p>/<li>."""
    # Use markdown-it's HTML-stripping behaviour by parsing through its inline rules
    # ... but really, regex is fine here for our needs.
    import re
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"</li>", "\n", text)
    text = re.sub(r"<li>", " - ", text)
    text = re.sub(r"<[^>]+>", "", text)
    # Collapse whitespace.
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()
