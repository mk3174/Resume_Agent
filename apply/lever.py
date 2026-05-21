"""Lever application form filler.

Lever uses /apply suffix on the posting URL. Field names:
  - name, email, phone, org, urls[LinkedIn], urls[GitHub], urls[Other]
  - resume (input[type=file][name='resume'])
  - additional info (textarea[name='comments'])
"""

from __future__ import annotations

import logging

from apply._common import (
    fill_lever_basics,
    fill_lever_standard_fields,
    fill_lever_card_fields,
    prepare_form_before_submit,
    submit_and_screenshot,
    upload_resume,
    _has_blocking_barriers,
)
from apply.base import (
    Applier,
    ApplyContext,
    ApplyResult,
    BrowserSession,
    register,
    safe_fill,
    screenshot_to,
)
from orchestrator.state import JobSource

log = logging.getLogger(__name__)


class LeverApplier(Applier):
    source = JobSource.LEVER

    def apply(self, ctx: ApplyContext) -> ApplyResult:
        url = str(ctx.job.apply_url)
        if not url.rstrip("/").endswith("/apply"):
            url = url.rstrip("/") + "/apply"

        with BrowserSession(source=self.source, headed=not ctx.dry_apply) as browser_ctx:
            page = browser_ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_selector("form#application-form, form", timeout=15000)
            except Exception as e:  # noqa: BLE001
                log.warning("no form found: %s", e)

            fill_lever_basics(page, ctx)
            fill_lever_standard_fields(page, ctx)
            fill_lever_card_fields(page, ctx)

            cover = (ctx.cover_md.read_text(encoding="utf-8") if ctx.cover_md.exists() else "").strip()
            if cover:
                safe_fill(page, "textarea[name='comments']", cover)

            upload_resume(
                page,
                ctx,
                selectors=[
                    "input[type='file'][name='resume']",
                    "input[type='file']",
                ],
            )
            qa, barriers = prepare_form_before_submit(page, ctx)
            if _has_blocking_barriers(barriers) and not ctx.dry_apply:
                png = screenshot_to(ctx.output_dir, page)
                return ApplyResult(
                    submitted=False,
                    qa=qa,
                    barriers=barriers,
                    screenshot_path=str(png),
                    confirmation_url=page.url,
                )
            res = submit_and_screenshot(
                page,
                ctx,
                submit_selectors=["button[data-qa='btn-submit']", "button[type='submit']"],
            )
            res.qa = qa
            res.barriers = res.barriers + barriers
            return res


register(LeverApplier())
