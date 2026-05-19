"""Indeed ingestor (Phase 4).

Indeed aggressively fingerprints Playwright; Patchright applies stealth patches
that handle the basic detections. We layer:

- **Optional residential proxy** from `settings.ingest.indeed.proxy_url`
  (env-expanded). Without one, Indeed will Cloudflare-challenge most cloud IPs
  and reasonably busy home IPs.
- **Persistent session** under `storage_state/indeed.json` so the search-results
  page doesn't reload the consent / region modal every run.
- **Rate limiting + jitter**: at most `max_listings_per_run` per sweep with a
  random sleep between listing clicks.
- **Ban detection**: Cloudflare challenge, captcha iframe, "Verify you are
  human" page all abort the sweep cleanly.
"""

from __future__ import annotations

import logging
import random
import time
import urllib.parse
from pathlib import Path
from typing import Iterable

from apply.base import detect_ban_signal
from config_loader import PROJECT_ROOT, load_settings
from orchestrator.state import JobPosting, JobSource

log = logging.getLogger(__name__)


class BanDetectedError(RuntimeError):
    """Raised when Indeed serves a CAPTCHA / Cloudflare wall."""


class IndeedIngestor:
    """Patchright-driven Indeed scraper, optional residential proxy."""

    source_name = "indeed"

    def fetch(self, board_token: str) -> Iterable[JobPosting]:
        """`board_token` is a comma-separated set of search keywords."""
        cfg = load_settings()["ingest"].get("indeed", {}) or {}
        if not cfg.get("enabled", False):
            log.info("Indeed ingest disabled in settings.yaml")
            return []

        keywords = [k.strip() for k in board_token.split(",") if k.strip()]
        if not keywords:
            return []

        try:
            return list(self._scrape(keywords, cfg))
        except BanDetectedError as e:
            log.error("Indeed ban detected, aborting sweep: %s", e)
            return []

    # ----------------------------------------------------------------------

    def _scrape(self, keywords: list[str], cfg: dict) -> Iterable[JobPosting]:
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            log.error("patchright not installed; install extras `[playwright]`")
            return []

        state_path = _resolve_storage_state(cfg)
        proxy_url = (cfg.get("proxy_url") or "").strip()
        max_jobs = int(cfg.get("max_listings_per_run", 20))
        min_delay = float(cfg.get("min_seconds_between_requests", 3.0))
        max_delay = float(cfg.get("max_seconds_between_requests", 7.0))
        location = cfg.get("location") or ""

        out: list[JobPosting] = []
        launch_kwargs: dict = {"headless": False}
        if proxy_url:
            launch_kwargs["proxy"] = {"server": proxy_url}

        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch_kwargs)
            ctx_kwargs: dict = {"viewport": {"width": 1366, "height": 900}}
            if state_path.exists():
                ctx_kwargs["storage_state"] = str(state_path)
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()

            for kw in keywords:
                if len(out) >= max_jobs:
                    break
                page.goto(_build_search_url(kw, location), wait_until="domcontentloaded", timeout=45000)
                _assert_no_ban(page)
                time.sleep(random.uniform(min_delay, max_delay))

                cards = page.locator("a.tapItem, a.result, a[data-jk]")
                count = min(cards.count(), max_jobs - len(out))
                for i in range(count):
                    card = cards.nth(i)
                    try:
                        jk = card.get_attribute("data-jk") or _jk_from_href(card.get_attribute("href") or "")
                        if not jk:
                            continue
                        card.scroll_into_view_if_needed(timeout=4000)
                        card.click(timeout=4000)
                        time.sleep(random.uniform(min_delay, max_delay))
                        _assert_no_ban(page)
                        posting = _parse_detail_panel(page, jk, kw)
                        if posting:
                            out.append(posting)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("Indeed: skip card %d (%s)", i, exc)
                        continue
                    if len(out) >= max_jobs:
                        break

            state_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(state_path))
            context.close()
            browser.close()

        log.info("Indeed ingestor: collected %d postings", len(out))
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_storage_state(cfg: dict) -> Path:
    rel = cfg.get("storage_state_path", "storage_state/indeed.json")
    p = Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _build_search_url(keywords: str, location: str) -> str:
    q = {"q": keywords, "fromage": "7", "sort": "date"}
    if location:
        q["l"] = location
    return "https://www.indeed.com/jobs?" + urllib.parse.urlencode(q)


def _assert_no_ban(page) -> None:
    ban = detect_ban_signal(page)
    if ban:
        raise BanDetectedError(ban)
    body = (page.content() or "").lower()
    for needle in (
        "cloudflare",
        "verify you are human",
        "your traffic looks similar to automated requests",
        "please solve this captcha",
    ):
        if needle in body:
            raise BanDetectedError(needle)


def _jk_from_href(href: str) -> str:
    import re

    m = re.search(r"[?&]jk=([a-f0-9]+)", href)
    return m.group(1) if m else ""


def _parse_detail_panel(page, jk: str, keywords: str) -> JobPosting | None:
    try:
        title = _text(page, "h2.jobsearch-JobInfoHeader-title, h1.jobsearch-JobInfoHeader-title")
        company = _text(page, "div[data-company-name='true'], [data-testid='inlineHeader-companyName']")
        location = _text(page, "[data-testid='inlineHeader-companyLocation'], .jobsearch-CompanyInfoContainer div")
        desc = _text(page, "#jobDescriptionText")
        apply_url = page.url
        return JobPosting(
            source=JobSource.INDEED,
            external_id=jk,
            company=company or "Unknown",
            title=title or "Unknown",
            location=location or None,
            description_text=desc or "",
            apply_url=apply_url,
            raw={"keywords": keywords, "jk": jk},
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("Indeed parse detail failed: %s", exc)
        return None


def _text(page, selector: str) -> str:
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return ""
        return (loc.inner_text(timeout=2500) or "").strip()
    except Exception:  # noqa: BLE001
        return ""
