"""LinkedIn Easy Apply form-filler.

Strategy:

1. Open the job posting in a logged-in Patchright context (persistent
   storage_state). If we land on /login, surface a barrier asking the user
   to re-run `resume-agent linkedin-login`.
2. Click the "Easy Apply" button. If it's absent, the job is an off-site
   redirect — return a barrier asking for manual application.
3. The Easy Apply modal is a wizard with N steps:
     - personal info (auto-fills from a stored profile most of the time)
     - resume upload
     - additional questions (custom Q&A -> responder)
     - review + submit
   We walk steps via `next` button until we see `submit application`.
4. Every step is wrapped in a ban-detection guard. If LinkedIn shows a
   security challenge, captcha, 2FA, or rate-limit prompt -> abort the
   submit and return a barrier of the appropriate kind. The orchestrator
   pings Telegram with the barrier and the page screenshot.
5. Conservative rate-limit: rely on `apply/rate_limit.py` (global hourly cap)
   plus per-step delays from `settings.yaml -> ingest.linkedin` (defaults mimic
   a slow human). Real submits also honour `min_seconds_between_easy_apply_submits`.
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path

from apply._common import answer_custom_questions
from apply.base import (
    Applier,
    ApplyContext,
    ApplyResult,
    detect_ban_signal,
    make_unrecoverable_barrier,
    register,
    safe_fill,
    screenshot_to,
)
from config_loader import PROJECT_ROOT, load_settings
from orchestrator.state import Barrier, BarrierKind, JobSource

log = logging.getLogger(__name__)


class LinkedInApplier(Applier):
    source = JobSource.LINKEDIN

    def apply(self, ctx: ApplyContext) -> ApplyResult:
        cfg = load_settings()["ingest"].get("linkedin", {}) or {}
        state_path = _resolve_storage_state(cfg)
        if not state_path.exists():
            return ApplyResult(
                submitted=False,
                barriers=[
                    Barrier(
                        kind=BarrierKind.UNKNOWN,
                        message="LinkedIn storage_state missing. Run `resume-agent linkedin-login`.",
                        context={"path": str(state_path)},
                    )
                ],
            )

        try:
            from patchright.sync_api import sync_playwright
        except ImportError as e:
            log.error("patchright not installed: %s", e)
            return ApplyResult(
                submitted=False,
                barriers=[make_unrecoverable_barrier("patchright not installed", error=str(e))],
            )

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context(
                storage_state=str(state_path),
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()
            try:
                page.goto(str(ctx.job.apply_url), wait_until="domcontentloaded", timeout=45000)
            except Exception as e:  # noqa: BLE001
                return _crash_result(ctx, page, f"navigation failed: {e}")

            # Same pacing knobs as job-search ingest: pause before interacting.
            time.sleep(
                random.uniform(
                    float(cfg.get("post_search_page_delay_min", 5.0)),
                    float(cfg.get("post_search_page_delay_max", 14.0)),
                )
            )

            login_barrier = _login_check(page)
            if login_barrier:
                return ApplyResult(submitted=False, barriers=[login_barrier])

            ban_barrier = _ban_check(page)
            if ban_barrier:
                return ApplyResult(submitted=False, barriers=[ban_barrier])

            if not ctx.dry_apply:
                cool = _submit_cooldown_barrier(cfg)
                if cool:
                    return ApplyResult(submitted=False, barriers=[cool])

            return self._run_wizard(page, ctx, context, state_path, cfg)

    # ------------------------------------------------------------------

    def _run_wizard(self, page, ctx: ApplyContext, context, state_path: Path, cfg: dict) -> ApplyResult:
        """Walk the Easy Apply modal step-by-step."""
        # 1) Click Easy Apply
        easy = page.locator("button:has-text('Easy Apply'), button[aria-label*='Easy Apply']").first
        if easy.count() == 0:
            return ApplyResult(
                submitted=False,
                barriers=[
                    Barrier(
                        kind=BarrierKind.UNKNOWN,
                        message="Easy Apply unavailable (off-site application). Apply manually.",
                        context={"url": page.url},
                    )
                ],
            )
        easy.click(timeout=10000)
        page.wait_for_selector("div.jobs-easy-apply-modal, [aria-label*='Apply']", timeout=15000)

        qa, barriers = [], []
        wizard_steps = 12  # hard upper bound to avoid infinite loops
        for step in range(wizard_steps):
            time.sleep(_wizard_step_delay(cfg))

            b = _ban_check(page)
            if b:
                return ApplyResult(
                    submitted=False,
                    qa=qa,
                    barriers=barriers + [b],
                    screenshot_path=str(screenshot_to(ctx.output_dir, page)),
                )

            # Resume upload step
            _maybe_upload_resume(page, ctx)
            # Custom questions
            new_qa, new_b = answer_custom_questions(page, ctx)
            qa.extend(new_qa)
            barriers.extend(new_b)
            if new_b and any(_is_blocking(barrier) for barrier in new_b):
                # Unanswered question; pause submit.
                return ApplyResult(
                    submitted=False,
                    qa=qa,
                    barriers=barriers,
                    screenshot_path=str(screenshot_to(ctx.output_dir, page)),
                )

            submit = page.locator(
                "button:has-text('Submit application'), button[aria-label='Submit application']"
            ).first
            if submit.count() > 0:
                # Final step
                return self._submit(page, ctx, context, state_path, submit, qa, barriers, cfg)

            next_btn = page.locator(
                "button:has-text('Next'), button[aria-label='Continue to next step']"
            ).first
            review_btn = page.locator(
                "button:has-text('Review'), button[aria-label*='Review']"
            ).first

            if next_btn.count() > 0:
                next_btn.click(timeout=10000)
            elif review_btn.count() > 0:
                review_btn.click(timeout=10000)
            else:
                # Nothing to click; bail.
                return ApplyResult(
                    submitted=False,
                    qa=qa,
                    barriers=barriers
                    + [make_unrecoverable_barrier("wizard stalled — no Next/Review/Submit", url=page.url)],
                    screenshot_path=str(screenshot_to(ctx.output_dir, page)),
                )

        return ApplyResult(
            submitted=False,
            qa=qa,
            barriers=barriers
            + [make_unrecoverable_barrier("Easy Apply wizard exceeded max steps", url=page.url)],
            screenshot_path=str(screenshot_to(ctx.output_dir, page)),
        )

    # ------------------------------------------------------------------

    def _submit(
        self,
        page,
        ctx: ApplyContext,
        context,
        state_path: Path,
        submit_locator,
        qa,
        barriers,
        cfg: dict,
    ) -> ApplyResult:
        _ = cfg  # reserved for future per-submit tuning
        if ctx.dry_apply:
            png = screenshot_to(ctx.output_dir, page)
            context.storage_state(path=str(state_path))
            return ApplyResult(
                submitted=False,
                qa=qa,
                barriers=barriers,
                screenshot_path=str(png),
                confirmation_url=page.url,
            )

        try:
            submit_locator.click(timeout=10000)
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception as e:  # noqa: BLE001
            return _crash_result(ctx, page, f"submit click failed: {e}", qa, barriers)

        b = _ban_check(page)
        if b:
            return ApplyResult(
                submitted=False, qa=qa, barriers=barriers + [b],
                screenshot_path=str(screenshot_to(ctx.output_dir, page)),
            )

        png = screenshot_to(ctx.output_dir, page)
        # Refresh storage_state so the cookie roll-over is captured.
        context.storage_state(path=str(state_path))
        _record_linkedin_submit_ts()
        return ApplyResult(
            submitted=True,
            qa=qa,
            barriers=barriers,
            screenshot_path=str(png),
            confirmation_url=page.url,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wizard_step_delay(cfg: dict) -> float:
    return random.uniform(
        float(cfg.get("easy_apply_step_delay_min", 6.0)),
        float(cfg.get("easy_apply_step_delay_max", 16.0)),
    )


def _submit_cooldown_barrier(cfg: dict) -> Barrier | None:
    """Block real submits if the last LinkedIn submit was too recent (local guard)."""
    min_s = float(cfg.get("min_seconds_between_easy_apply_submits") or 0)
    if min_s <= 0:
        return None
    path = PROJECT_ROOT / "data" / ".linkedin_last_submit_at.json"
    if not path.exists():
        return None
    try:
        last = float(json.loads(path.read_text(encoding="utf-8")).get("unix", 0))
    except Exception:  # noqa: BLE001
        return None
    elapsed = time.time() - last
    if elapsed >= min_s:
        return None
    wait_s = min_s - elapsed
    return Barrier(
        kind=BarrierKind.UNKNOWN,
        message=(
            "Local LinkedIn submit cooldown active "
            f"({wait_s / 60:.1f} min remaining) — protects your account from rapid automated applies."
        ),
        context={"min_seconds_between_easy_apply_submits": min_s, "wait_s": wait_s},
    )


def _record_linkedin_submit_ts() -> None:
    path = PROJECT_ROOT / "data" / ".linkedin_last_submit_at.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"unix": time.time()}), encoding="utf-8")


def _resolve_storage_state(cfg: dict) -> Path:
    rel = cfg.get("storage_state_path", "storage_state/linkedin.json")
    p = Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _login_check(page) -> Barrier | None:
    url = page.url
    if "/login" in url or "/uas/login" in url or "checkpoint" in url:
        return Barrier(
            kind=BarrierKind.TWO_FACTOR if "checkpoint" in url else BarrierKind.UNKNOWN,
            message="LinkedIn redirected to login/checkpoint — session expired or 2FA required.",
            context={"url": url},
        )
    return None


def _ban_check(page) -> Barrier | None:
    ban = detect_ban_signal(page)
    if ban:
        return Barrier(kind=BarrierKind.BAN_SIGNAL, message=f"ban signal: {ban}", context={"url": page.url})
    body = (page.content() or "").lower()
    if "security verification" in body or "let's do a quick security check" in body:
        return Barrier(kind=BarrierKind.CAPTCHA, message="LinkedIn security verification", context={"url": page.url})
    if "your account has been temporarily restricted" in body:
        return Barrier(kind=BarrierKind.BAN_SIGNAL, message="account restricted", context={"url": page.url})
    return None


def _is_blocking(b: Barrier) -> bool:
    return b.kind in (BarrierKind.LOW_CONF_QA, BarrierKind.MISSING_FACT, BarrierKind.CAPTCHA, BarrierKind.TWO_FACTOR)


def _maybe_upload_resume(page, ctx: ApplyContext) -> None:
    """The Easy Apply modal sometimes asks for a fresh resume upload."""
    sels = [
        "input[type='file'][name*='resume']",
        "input[type='file'][id*='resume']",
        "input[type='file']",
    ]
    for s in sels:
        try:
            loc = page.locator(s).first
            if loc.count() == 0:
                continue
            loc.set_input_files(str(ctx.resume_pdf))
            log.info("LinkedIn: uploaded resume via %s", s)
            return
        except Exception as e:  # noqa: BLE001
            log.debug("upload via %s skipped: %s", s, e)


def _crash_result(ctx: ApplyContext, page, msg: str, qa=None, barriers=None) -> ApplyResult:
    png = None
    try:
        png = str(screenshot_to(ctx.output_dir, page))
    except Exception:  # noqa: BLE001
        pass
    return ApplyResult(
        submitted=False,
        qa=qa or [],
        barriers=(barriers or []) + [make_unrecoverable_barrier(msg, url=getattr(page, "url", ""))],
        screenshot_path=png,
    )


# Side-effect register on import (mirrors greenhouse / lever / ashby / workday).
register(LinkedInApplier())

# Quiet a lint warning about safe_fill being imported but unused.
_ = safe_fill
