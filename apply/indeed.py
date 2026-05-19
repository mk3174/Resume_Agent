"""Indeed applier.

Indeed apply flows fall into three buckets:

1. **Indeed Apply (on-platform)**: a modal/inline form on indeed.com that's
   structurally similar to Greenhouse / Lever. We fill it the same way.
2. **External apply** (button text "Apply on company site"): redirects off
   indeed.com. We do NOT chase those — return a barrier asking the user to
   apply manually (the ATS-specific applier should have caught those at
   ingest time).
3. **Sponsored applies** with custom screening questions: handled by the
   shared `answer_custom_questions` helper from `apply/_common.py`.

The applier honours optional `proxy_url` from settings (residential proxies
materially reduce Indeed's Cloudflare-challenge rate) and surfaces CAPTCHA
or rate-limit pages as barriers.
"""

from __future__ import annotations

import logging
from pathlib import Path

from apply._common import answer_custom_questions, fill_common, upload_resume
from apply.base import (
    Applier,
    ApplyContext,
    ApplyResult,
    detect_ban_signal,
    make_unrecoverable_barrier,
    register,
    screenshot_to,
)
from config_loader import PROJECT_ROOT, load_settings
from orchestrator.state import Barrier, BarrierKind, JobSource

log = logging.getLogger(__name__)


class IndeedApplier(Applier):
    source = JobSource.INDEED

    def apply(self, ctx: ApplyContext) -> ApplyResult:
        cfg = load_settings()["ingest"].get("indeed", {}) or {}
        state_path = _resolve_storage_state(cfg)
        proxy_url = (cfg.get("proxy_url") or "").strip()

        try:
            from patchright.sync_api import sync_playwright
        except ImportError as e:
            return ApplyResult(
                submitted=False,
                barriers=[make_unrecoverable_barrier("patchright not installed", error=str(e))],
            )

        launch: dict = {"headless": False}
        if proxy_url:
            launch["proxy"] = {"server": proxy_url}

        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch)
            ctx_kwargs: dict = {"viewport": {"width": 1366, "height": 900}}
            if state_path.exists():
                ctx_kwargs["storage_state"] = str(state_path)
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()
            try:
                page.goto(str(ctx.job.apply_url), wait_until="domcontentloaded", timeout=45000)
            except Exception as e:  # noqa: BLE001
                return _crash(ctx, page, f"navigation failed: {e}")

            b = _ban(page)
            if b:
                return ApplyResult(submitted=False, barriers=[b])

            # Click "Apply now" button on the listing.
            apply_btn = page.locator(
                "button:has-text('Apply now'), a[aria-label*='Apply now'], #indeedApplyButton"
            ).first
            if apply_btn.count() == 0:
                return ApplyResult(
                    submitted=False,
                    barriers=[
                        Barrier(
                            kind=BarrierKind.UNKNOWN,
                            message="No Indeed Apply button (likely external apply). Apply manually.",
                            context={"url": page.url},
                        )
                    ],
                )
            apply_btn.click(timeout=10000)
            page.wait_for_load_state("networkidle", timeout=20000)

            # External apply detection: URL leaves indeed.com.
            if "indeed.com" not in page.url and "smartapply.indeed.com" not in page.url:
                return ApplyResult(
                    submitted=False,
                    barriers=[
                        Barrier(
                            kind=BarrierKind.UNKNOWN,
                            message=f"Indeed redirected to external site: {page.url}",
                            context={"url": page.url},
                        )
                    ],
                )

            return self._fill_form(page, ctx, context, state_path)

    # ------------------------------------------------------------------

    def _fill_form(self, page, ctx: ApplyContext, context, state_path: Path) -> ApplyResult:
        try:
            page.wait_for_selector("form, [data-testid='ApplicationFormContainer']", timeout=15000)
        except Exception as e:  # noqa: BLE001
            log.warning("Indeed form did not render: %s", e)

        # Indeed often pre-fills from your saved profile; fill_common runs anyway
        # so missing fields get covered.
        fill_common(page, ctx)
        upload_resume(
            page,
            ctx,
            selectors=[
                "input[type='file'][data-testid*='resume']",
                "input[type='file'][name*='resume']",
                "input[type='file']",
            ],
        )

        qa, barriers = answer_custom_questions(page, ctx)

        b = _ban(page)
        if b:
            return ApplyResult(
                submitted=False,
                qa=qa,
                barriers=barriers + [b],
                screenshot_path=str(screenshot_to(ctx.output_dir, page)),
            )

        # Walk Indeed's multi-page flow: "Continue" until "Submit application".
        for _ in range(10):
            b = _ban(page)
            if b:
                return ApplyResult(
                    submitted=False,
                    qa=qa,
                    barriers=barriers + [b],
                    screenshot_path=str(screenshot_to(ctx.output_dir, page)),
                )

            submit = page.locator(
                "button:has-text('Submit your application'), button:has-text('Submit application')"
            ).first
            if submit.count() > 0:
                return self._submit(page, ctx, context, state_path, submit, qa, barriers)

            cont = page.locator("button:has-text('Continue'), button:has-text('Review your application')").first
            if cont.count() == 0:
                break
            cont.click(timeout=10000)
            page.wait_for_load_state("networkidle", timeout=15000)
            # Run the QA helper again for any new step.
            new_qa, new_b = answer_custom_questions(page, ctx)
            qa.extend(new_qa)
            barriers.extend(new_b)

        return ApplyResult(
            submitted=False,
            qa=qa,
            barriers=barriers
            + [make_unrecoverable_barrier("Indeed apply flow stalled", url=page.url)],
            screenshot_path=str(screenshot_to(ctx.output_dir, page)),
        )

    def _submit(self, page, ctx: ApplyContext, context, state_path: Path, submit_locator, qa, barriers) -> ApplyResult:
        if ctx.dry_apply:
            png = screenshot_to(ctx.output_dir, page)
            state_path.parent.mkdir(parents=True, exist_ok=True)
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
            return _crash(ctx, page, f"submit failed: {e}", qa, barriers)
        b = _ban(page)
        if b:
            return ApplyResult(
                submitted=False,
                qa=qa,
                barriers=barriers + [b],
                screenshot_path=str(screenshot_to(ctx.output_dir, page)),
            )
        png = screenshot_to(ctx.output_dir, page)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(state_path))
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


def _resolve_storage_state(cfg: dict) -> Path:
    rel = cfg.get("storage_state_path", "storage_state/indeed.json")
    p = Path(rel)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _ban(page) -> Barrier | None:
    ban = detect_ban_signal(page)
    if ban:
        return Barrier(kind=BarrierKind.BAN_SIGNAL, message=f"ban signal: {ban}", context={"url": page.url})
    body = (page.content() or "").lower()
    if "cloudflare" in body and "challenge" in body:
        return Barrier(kind=BarrierKind.CAPTCHA, message="Cloudflare challenge", context={"url": page.url})
    if "verify you are human" in body:
        return Barrier(kind=BarrierKind.CAPTCHA, message="hCaptcha / verification", context={"url": page.url})
    return None


def _crash(ctx: ApplyContext, page, msg: str, qa=None, barriers=None) -> ApplyResult:
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


register(IndeedApplier())
