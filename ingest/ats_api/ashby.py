"""Ashby public Posting API.

Endpoint: https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true
No auth.
"""

from __future__ import annotations

import logging
from typing import Iterable

import httpx

from orchestrator.state import JobPosting, JobSource

log = logging.getLogger(__name__)

BASE = "https://api.ashbyhq.com/posting-api/job-board/{token}"
TIMEOUT = httpx.Timeout(20.0)


class AshbyIngestor:
    source_name = "ashby"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "resume-agent/0.1"})

    def fetch(self, board_token: str) -> Iterable[JobPosting]:
        url = BASE.format(token=board_token) + "?includeCompensation=true"
        try:
            r = self._client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("ashby fetch failed for %s: %s", board_token, e)
            return []

        data = r.json()
        for raw in data.get("jobs", []):
            yield self._parse(raw, board_token)

    @staticmethod
    def _parse(raw: dict, company: str) -> JobPosting:
        comp = raw.get("compensation") or {}
        salary_min = salary_max = None
        # Ashby returns compensation tiers; use the first USD one if present.
        for tier in comp.get("compensationTierSummary", []) or []:
            cur = (tier.get("currencyCode") or "").upper()
            if cur == "USD":
                salary_min = tier.get("minValue")
                salary_max = tier.get("maxValue")
                break

        desc = raw.get("descriptionPlain") or _strip_html(raw.get("descriptionHtml", ""))
        return JobPosting(
            source=JobSource.ASHBY,
            external_id=raw["id"],
            company=raw.get("organizationName") or company.title(),
            title=raw["title"],
            location=raw.get("locationName"),
            employment_type=raw.get("employmentType"),
            department=raw.get("departmentName") or raw.get("teamName"),
            salary_min_usd=int(salary_min) if salary_min else None,
            salary_max_usd=int(salary_max) if salary_max else None,
            description_text=desc.strip(),
            description_html=raw.get("descriptionHtml"),
            apply_url=raw.get("jobUrl") or raw.get("applyUrl") or "",
            raw=raw,
        )


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"</li>", "\n", text)
    text = re.sub(r"<li>", " - ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
