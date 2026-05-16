"""Greenhouse application form filler.

Greenhouse forms have stable name attrs:
  - first_name, last_name, email, phone
  - resume (input[type=file] with id like 's3_upload_for_resume')
  - one custom-question section per job
  - submit button id = #submit_app
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
)
from orchestrator.state import JobSource

log = logging.getLogger(__name__)


class GreenhouseApplier(Applier):
    source = JobSource.GREENHOUSE

    def apply(self, ctx: ApplyContext) -> ApplyResult:
        with BrowserSession(source=self.source, headed=not ctx.dry_apply) as browser_ctx:
            page = browser_ctx.new_page()
            page.goto(str(ctx.job.apply_url), wait_until="domcontentloaded", timeout=30000)

            # Greenhouse usually inlines the form; some companies wrap it in an iframe.
            try:
                page.wait_for_selector("form", timeout=15000)
            except Exception as e:  # noqa: BLE001
                log.warning("no form found: %s", e)

            fill_common(page, ctx)
            upload_resume(
                page,
                ctx,
                selectors=[
                    "input[type='file'][id*='resume']",
                    "input[type='file'][name*='resume']",
                    "input[type='file']",
                ],
            )
            qa, barriers = answer_custom_questions(page, ctx)
            res = submit_and_screenshot(
                page,
                ctx,
                submit_selectors=["#submit_app", "button#submit_app", "button[type='submit']"],
            )
            res.qa = qa
            res.barriers = res.barriers + barriers
            return res


register(GreenhouseApplier())
