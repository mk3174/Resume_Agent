"""LinkedIn ingestor (Phase 4).

LinkedIn does not expose a public, stable jobs API. We log in once headed and
let Patchright persist `storage_state.json`; subsequent runs hydrate the same
session and scrape the public/job-search URLs from a logged-in context.

Design rules (all driven by `settings.yaml -> ingest.linkedin`):

- **Persistent session**: one storage_state JSON per platform under
  `storage_state/linkedin.json`. We never log in programmatically; if the
  session is missing or expired, we surface a Telegram barrier asking the user
  to run `uv run resume-agent linkedin-login` (headed) once. This avoids
  embedding LinkedIn credentials in the agent.

- **Conservative rate limiting**: at most `ingest.linkedin.max_listings_per_run`
  jobs per sweep (default 8) plus long random gaps between job-detail loads,
  a pause after each search `goto`, and slower scroll-wheel pacing. The whole
  sweep aborts on any ban signal.

- **Ingest breadth cap**: `max_searches_per_ingest` limits how many keyword rows
  run per `resume-agent ingest` (each row is a separate browser session).

- **Ban-detection circuit breaker**: every page load runs through
  `apply.base.detect_ban_signal` plus LinkedIn-specific heuristics (login wall
  redirect, "Let's do a quick security check", 429). On hit, raise
  `BanDetectedError` and stop the sweep.

- **No LLM calls**: pure scraping. Description text is normalised the same way
  the Greenhouse ingestor does, so downstream Tailor sees identical input.
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
    """Raised when LinkedIn returns a ban / login-wall / security-check page."""


class SessionExpiredError(RuntimeError):
    """Raised when the storage_state no longer authenticates."""


# ---------------------------------------------------------------------------
# Public ingestor
# ---------------------------------------------------------------------------


class LinkedInIngestor:
    """Scrapes LinkedIn jobs search using a persistent logged-in session."""

    source_name = "linkedin"

    def fetch(self, board_token: str) -> Iterable[JobPosting]:
        """`board_token` is a comma-separated set of keywords for jobs search,
        e.g. ``"ai engineer,llm engineer"``. Locations / filters come from
        settings.ingest.linkedin.
        """
        cfg = load_settings()["ingest"].get("linkedin", {}) or {}
        if not cfg.get("enabled", False):
            log.info("LinkedIn ingest disabled in settings.yaml")
            return []

        keywords = [k.strip() for k in board_token.split(",") if k.strip()]
        if not keywords:
            log.warning("LinkedIn ingestor: empty keywords")
            return []

        try:
            return list(self._scrape(keywords, cfg))
        except BanDetectedError as e:
            log.error("LinkedIn ban detected, aborting sweep: %s", e)
            return []
        except SessionExpiredError as e:
            log.error("LinkedIn session expired: %s; run `resume-agent linkedin-login`.", e)
            return []

    # ----------------------------------------------------------------------

    def _scrape(self, keywords: list[str], cfg: dict) -> Iterable[JobPosting]:
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            log.error("patchright not installed; install extras `[playwright]`")
            raise SessionExpiredError("patchright missing") from e

        state_path = _resolve_storage_state(cfg)
        if not state_path.exists():
            raise SessionExpiredError(
                f"No LinkedIn storage_state at {state_path}. "
                "Run `uv run resume-agent linkedin-login` once (headed) to create one."
            )

        max_jobs = int(cfg.get("max_listings_per_run", 8))
        min_delay = float(cfg.get("min_seconds_between_requests", 15.0))
        max_delay = float(cfg.get("max_seconds_between_requests", 35.0))
        post_goto_min = float(cfg.get("post_search_page_delay_min", 5.0))
        post_goto_max = float(cfg.get("post_search_page_delay_max", 14.0))
        location = cfg.get("location") or ""

        out: list[JobPosting] = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context(
                storage_state=str(state_path),
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()

            for kw in keywords:
                if len(out) >= max_jobs:
                    break
                search_url = _build_search_url(kw, location)
                log.info("LinkedIn: %s", search_url)
                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(random.uniform(post_goto_min, post_goto_max))
                _assert_logged_in(page)
                _assert_no_ban(page)

                # The job-cards list virtualises; scroll to load more.
                _scroll_jobs_list(page, max_jobs - len(out), cfg)
                cards = page.locator("li[data-occludable-job-id]")
                n = min(cards.count(), max_jobs - len(out))
                for i in range(n):
                    card = cards.nth(i)
                    try:
                        card.scroll_into_view_if_needed(timeout=5000)
                        card.click(timeout=5000)
                        # Let the right-hand panel fill in.
                        time.sleep(random.uniform(min_delay, max_delay))
                        posting = _parse_detail_panel(page, kw)
                        if posting:
                            out.append(posting)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("LinkedIn: skip card %d (%s)", i, exc)
                        continue
                    if len(out) >= max_jobs:
                        break
                    _assert_no_ban(page)

            # Persist updated storage_state (cookies refreshed during scroll).
            context.storage_state(path=str(state_path))
            context.close()
            browser.close()

        log.info("LinkedIn ingestor: collected %d postings", len(out))
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_storage_state(cfg: dict) -> Path:
    rel = cfg.get("storage_state_path", "storage_state/linkedin.json")
    p = Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _build_search_url(keywords: str, location: str) -> str:
    q = {"keywords": keywords, "f_AL": "true", "sortBy": "DD"}  # f_AL = easy apply
    if location:
        q["location"] = location
    return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(q)


def _assert_logged_in(page) -> None:
    # LinkedIn redirects logged-out users to /login or shows the "join now" wall.
    url = page.url
    if "/login" in url or "/uas/login" in url or "checkpoint" in url:
        raise SessionExpiredError(f"redirected to login: {url}")


def _assert_no_ban(page) -> None:
    ban = detect_ban_signal(page)
    if ban:
        raise BanDetectedError(ban)
    body = (page.content() or "").lower()
    for needle in (
        "security verification",
        "let's do a quick security check",
        "your account has been temporarily restricted",
        "unusual activity",
    ):
        if needle in body:
            raise BanDetectedError(needle)


def _scroll_jobs_list(page, target: int, cfg: dict | None = None) -> None:
    """LinkedIn lazy-loads job cards as you scroll the left panel."""
    cfg = cfg or {}
    smin = float(cfg.get("scroll_step_delay_min", 2.2))
    smax = float(cfg.get("scroll_step_delay_max", 5.0))
    last_seen = 0
    stagnant = 0
    for _ in range(20):
        page.mouse.wheel(0, 1500)
        time.sleep(random.uniform(smin, smax))
        n = page.locator("li[data-occludable-job-id]").count()
        if n >= target:
            return
        if n == last_seen:
            stagnant += 1
            if stagnant >= 3:
                return
        else:
            stagnant = 0
            last_seen = n


def _parse_detail_panel(page, keywords: str) -> JobPosting | None:
    """Pull title/company/location/description from the right-hand details panel."""
    try:
        title = _text(page, "h1.t-24, .jobs-unified-top-card__job-title, h1")
        company = _text(page, ".jobs-unified-top-card__company-name a, .jobs-unified-top-card__company-name")
        location = _text(page, ".jobs-unified-top-card__bullet, .jobs-unified-top-card__primary-description")
        desc = _text(page, ".jobs-description-content__text, #job-details, .jobs-description__content")
        external_id = _job_id_from_url(page.url)
        apply_url = page.url

        if not title or not external_id:
            return None

        return JobPosting(
            source=JobSource.LINKEDIN,
            external_id=external_id,
            company=company or "Unknown",
            title=title,
            location=location or None,
            description_text=desc or "",
            apply_url=apply_url,
            raw={"keywords": keywords},
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("parse detail panel failed: %s", exc)
        return None


def _text(page, selector: str) -> str:
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return ""
        return (loc.inner_text(timeout=2500) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _job_id_from_url(url: str) -> str:
    """LinkedIn job URLs end with /jobs/view/<id>/ or have currentJobId=<id>."""
    import re

    m = re.search(r"/jobs/view/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"currentJobId=(\d+)", url)
    if m:
        return m.group(1)
    return url.rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# CLI helper: one-time headed login
# ---------------------------------------------------------------------------


def interactive_login() -> Path:
    """Open a headed browser at LinkedIn, let the user sign in by hand, save
    the resulting storage_state. Idempotent — overwrites any prior file.
    """
    cfg = load_settings()["ingest"].get("linkedin", {}) or {}
    state_path = _resolve_storage_state(cfg)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    from patchright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        # Block until LinkedIn loads the feed (user has logged in).
        try:
            page.wait_for_url("**/feed/**", timeout=300_000)
        except Exception:  # noqa: BLE001
            # Even if we don't hit /feed/, save whatever cookies exist so a
            # partial session can be inspected.
            pass
        ctx.storage_state(path=str(state_path))
        ctx.close()
        browser.close()
    log.info("Saved LinkedIn storage_state to %s", state_path)
    return state_path
