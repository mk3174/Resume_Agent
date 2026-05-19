"""Ashby public Posting API.

Endpoint: https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true
No auth.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import httpx

from orchestrator.state import JobPosting, JobSource

log = logging.getLogger(__name__)

BASE = "https://api.ashbyhq.com/posting-api/job-board/{token}"
TIMEOUT = httpx.Timeout(20.0)


def _usd_salary_range(comp: Any) -> tuple[int | None, int | None]:
    """Extract USD salary min/max from Ashby ``compensation`` object.

    Ashby has evolved the shape:
    - **Legacy:** ``compensationTierSummary`` is a list of tier dicts with
      ``currencyCode``, ``minValue``, ``maxValue``.
    - **Current (e.g. OpenAI):** ``compensationTierSummary`` is a human string;
      numeric values live on ``summaryComponents`` or nested
      ``compensationTiers[].components[]``.
    """
    if not isinstance(comp, dict):
        return None, None

    def from_salary_row(row: dict) -> tuple[int | None, int | None] | None:
        if (row.get("currencyCode") or "").upper() != "USD":
            return None
        if row.get("compensationType") != "Salary":
            return None
        mn, mx = row.get("minValue"), row.get("maxValue")
        if mn is None and mx is None:
            return None
        return (
            int(mn) if mn is not None else None,
            int(mx) if mx is not None else None,
        )

    for row in comp.get("summaryComponents") or []:
        if isinstance(row, dict):
            got = from_salary_row(row)
            if got is not None:
                return got
    for tier in comp.get("compensationTiers") or []:
        if not isinstance(tier, dict):
            continue
        for row in tier.get("components") or []:
            if isinstance(row, dict):
                got = from_salary_row(row)
                if got is not None:
                    return got

    legacy = comp.get("compensationTierSummary")
    if isinstance(legacy, list):
        for tier in legacy:
            if not isinstance(tier, dict):
                continue
            if (tier.get("currencyCode") or "").upper() == "USD":
                mn, mx = tier.get("minValue"), tier.get("maxValue")
                return (
                    int(mn) if mn is not None else None,
                    int(mx) if mx is not None else None,
                )
    return None, None


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
        if not isinstance(data, dict):
            log.warning("ashby unexpected JSON root for %s: %s", board_token, type(data).__name__)
            return []
        for raw in data.get("jobs", []) or []:
            if not isinstance(raw, dict):
                log.warning("ashby skipping non-object job on %s", board_token)
                continue
            yield self._parse(raw, board_token)

    @staticmethod
    def _parse(raw: dict, company: str) -> JobPosting:
        comp = raw.get("compensation")
        salary_min, salary_max = _usd_salary_range(comp if isinstance(comp, dict) else None)

        desc = raw.get("descriptionPlain") or _strip_html(raw.get("descriptionHtml", ""))
        return JobPosting(
            source=JobSource.ASHBY,
            external_id=raw["id"],
            company=raw.get("organizationName") or company.title(),
            title=raw["title"],
            location=raw.get("locationName") or raw.get("location"),
            employment_type=raw.get("employmentType"),
            department=raw.get("departmentName") or raw.get("department") or raw.get("teamName") or raw.get("team"),
            salary_min_usd=salary_min,
            salary_max_usd=salary_max,
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
