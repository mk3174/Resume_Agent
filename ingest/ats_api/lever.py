"""Lever public Postings API.

Endpoint: https://api.lever.co/v0/postings/{token}?mode=json
No auth.
"""

from __future__ import annotations

import logging
from typing import Iterable

import httpx

from orchestrator.state import JobPosting, JobSource

log = logging.getLogger(__name__)

BASE = "https://api.lever.co/v0/postings/{token}"
TIMEOUT = httpx.Timeout(20.0)


class LeverIngestor:
    source_name = "lever"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "resume-agent/0.1"})

    def fetch(self, board_token: str) -> Iterable[JobPosting]:
        url = BASE.format(token=board_token) + "?mode=json"
        try:
            r = self._client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("lever fetch failed for %s: %s", board_token, e)
            return []

        for raw in r.json():
            yield self._parse(raw, board_token)

    @staticmethod
    def _parse(raw: dict, company: str) -> JobPosting:
        cats = raw.get("categories") or {}
        # Lever description is HTML in `description` and structured `lists`.
        desc_text = _strip_html(raw.get("descriptionPlain") or raw.get("description") or "")
        for lst in raw.get("lists", []):
            desc_text += "\n\n" + lst.get("text", "") + "\n" + _strip_html(lst.get("content", ""))
        return JobPosting(
            source=JobSource.LEVER,
            external_id=raw["id"],
            company=company.title(),
            title=raw.get("text") or raw.get("title", "?"),
            location=cats.get("location"),
            employment_type=cats.get("commitment"),
            department=cats.get("team") or cats.get("department"),
            description_text=desc_text.strip(),
            description_html=raw.get("description"),
            apply_url=raw.get("hostedUrl") or raw.get("applyUrl") or "",
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
