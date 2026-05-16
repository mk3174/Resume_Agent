"""Lever application form filler.

Lever uses /apply suffix on the posting URL. Field names:
  - name, email, phone, org, urls[LinkedIn], urls[GitHub], urls[Other]
  - resume (input[type=file][name='resume'])
  - additional info (textarea[name='comments'])
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

            # Lever uses combined `name` rather than first/last.
            full_name = ctx.master.name
            safe_fill(page, "[name='name']", full_name)
            fill_common(page, ctx)
            # Lever URL fields are bracketed.
            contact = ctx.master.contact or {}
            for key, val in (
                ("LinkedIn", contact.get("linkedin")),
                ("GitHub", contact.get("github")),
                ("Other", contact.get("portfolio")),
            ):
                if val:
                    safe_fill(page, f"[name='urls[{key}]']", val)

            # Cover letter goes into 'comments' textarea.
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
            qa, barriers = answer_custom_questions(page, ctx)
            res = submit_and_screenshot(
                page,
                ctx,
                submit_selectors=["button[data-qa='btn-submit']", "button[type='submit']"],
            )
            res.qa = qa
            res.barriers = res.barriers + barriers
            return res


register(LeverApplier())
