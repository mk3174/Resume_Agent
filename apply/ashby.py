"""Ashby application form filler.

Ashby renders forms in a SPA. Field selectors are React-generated, so we
rely on `aria-label` and label-text matching more than name attributes.
"""

from __future__ import annotations

import logging

from apply._common import (
    answer_custom_questions,
    fill_common,
    submit_and_screenshot,
    upload_resume,
)
from apply.base import (
    Applier,
    ApplyContext,
    ApplyResult,
    BrowserSession,
    register,
    safe_fill,
)
from orchestrator.state import JobSource

log = logging.getLogger(__name__)


class AshbyApplier(Applier):
    source = JobSource.ASHBY

    def apply(self, ctx: ApplyContext) -> ApplyResult:
        url = str(ctx.job.apply_url)
        if "/application" not in url:
            url = url.rstrip("/") + "/application"

        with BrowserSession(source=self.source, headed=not ctx.dry_apply) as browser_ctx:
            page = browser_ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_selector("form, [data-testid='application-form']", timeout=15000)
            except Exception as e:  # noqa: BLE001
                log.warning("no form found: %s", e)

            # Ashby uses aria-label primarily.
            full_name = ctx.master.name
            safe_fill(page, "input[aria-label*='Full Name']", full_name)
            safe_fill(page, "input[aria-label*='Email']", (ctx.master.contact or {}).get("email", ""))
            fill_common(page, ctx)
            upload_resume(page, ctx)
            qa, barriers = answer_custom_questions(page, ctx)
            res = submit_and_screenshot(
                page,
                ctx,
                submit_selectors=[
                    "button:has-text('Submit Application')",
                    "button:has-text('Submit')",
                    "button[type='submit']",
                ],
            )
            res.qa = qa
            res.barriers = res.barriers + barriers
            return res


register(AshbyApplier())
