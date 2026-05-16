"""Shared form-fill logic used by Greenhouse / Lever / Ashby appliers.

These three platforms render very similar single-page application forms:
  - first/last/email/phone fields with predictable name attrs
  - resume upload field (input[type=file])
  - LinkedIn / portfolio URL fields
  - a list of custom questions (textarea or radio groups)
  - a submit button

This file centralises that pattern so per-portal modules only override
selectors that genuinely differ.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from apply.base import (
    ApplyContext,
    ApplyResult,
    detect_ban_signal,
    iter_form_fields,
    make_unrecoverable_barrier,
    safe_fill,
    screenshot_to,
)
from notify.telegram import barrier_pause
from orchestrator.state import (
    Barrier,
    BarrierKind,
    QAEntry,
)
from pipeline import responder as responder_mod

log = logging.getLogger(__name__)


COMMON_FIELDS = {
    "first_name": ["[name='first_name']", "[name='firstName']", "input[id*='first_name']"],
    "last_name": ["[name='last_name']", "[name='lastName']", "input[id*='last_name']"],
    "email": ["[name='email']", "input[type='email']"],
    "phone": ["[name='phone']", "input[type='tel']"],
    "linkedin": ["[name*='linkedin']", "input[id*='linkedin']"],
    "github": ["[name*='github']", "input[id*='github']"],
    "portfolio": ["[name*='website']", "[name*='portfolio']"],
}


def fill_common(page, ctx: ApplyContext) -> None:
    """Fill name/email/phone/links from MasterResume contact info."""
    contact = ctx.master.contact or {}
    name_parts = ctx.master.name.split(" ", 1)
    first = name_parts[0]
    last = name_parts[1] if len(name_parts) > 1 else ""

    values = {
        "first_name": first,
        "last_name": last,
        "email": contact.get("email", ""),
        "phone": contact.get("phone", ""),
        "linkedin": contact.get("linkedin", ""),
        "github": contact.get("github", ""),
        "portfolio": contact.get("portfolio", ""),
    }
    for key, sels in COMMON_FIELDS.items():
        val = values.get(key)
        if not val:
            continue
        for s in sels:
            if safe_fill(page, s, val):
                break


def upload_resume(page, ctx: ApplyContext, *, selectors: Iterable[str] | None = None) -> bool:
    """Find the resume upload <input> and attach the tailored PDF."""
    sels = list(selectors or [
        "input[type='file'][name*='resume']",
        "input[type='file'][id*='resume']",
        "input[type='file']",  # last-resort first-file fallback
    ])
    for s in sels:
        try:
            loc = page.locator(s).first
            if loc.count() == 0:
                continue
            loc.set_input_files(str(ctx.resume_pdf))
            log.info("uploaded resume via %s", s)
            return True
        except Exception as e:  # noqa: BLE001
            log.debug("upload via %s failed: %s", s, e)
            continue
    return False


def answer_custom_questions(page, ctx: ApplyContext) -> tuple[list[QAEntry], list[Barrier]]:
    """Walk visible form labels, answer each via Responder, fill or barrier-park."""
    qa: list[QAEntry] = []
    barriers: list[Barrier] = []

    for sel, label, tag in iter_form_fields(page):
        # Skip the obvious common fields we already filled.
        lab_lower = label.lower()
        if any(k in lab_lower for k in ("first name", "last name", "email", "phone",
                                         "linkedin", "github", "website", "portfolio",
                                         "resume", "cv")):
            continue

        entry = responder_mod.answer(label, job_title=ctx.job.title, company=ctx.job.company)
        qa.append(entry)
        if entry.needs_review or not entry.answer:
            b = Barrier(
                kind=BarrierKind.LOW_CONF_QA if entry.source == "llm" else BarrierKind.MISSING_FACT,
                message=f"Need answer for: {label}",
                context={"selector": sel, "tag": tag, "url": page.url},
            )
            barriers.append(b)
            human = barrier_pause(b)
            if human:
                entry.answer = human
                entry.needs_review = False
                entry.source = "human"
                responder_mod.remember(entry)
        if entry.answer:
            try:
                if tag == "select":
                    page.locator(sel).first.select_option(label=entry.answer)
                else:
                    page.locator(sel).first.fill(entry.answer)
            except Exception as e:  # noqa: BLE001
                log.warning("could not fill %s: %s", sel, e)

    return qa, barriers


def submit_and_screenshot(
    page,
    ctx: ApplyContext,
    *,
    submit_selectors: Iterable[str] | None = None,
) -> ApplyResult:
    """Click the submit button, screenshot, return ApplyResult."""
    if ctx.dry_apply:
        png = screenshot_to(ctx.output_dir, page)
        return ApplyResult(
            submitted=False,
            screenshot_path=str(png),
            confirmation_url=page.url,
        )

    sels = list(submit_selectors or [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Submit')",
        "button:has-text('Apply')",
    ])

    ban = detect_ban_signal(page)
    if ban:
        return ApplyResult(
            submitted=False,
            barriers=[Barrier(kind=BarrierKind.BAN_SIGNAL, message=f"ban signal: {ban}",
                              context={"url": page.url})],
        )

    for s in sels:
        try:
            btn = page.locator(s).first
            if btn.count() == 0:
                continue
            btn.click()
            page.wait_for_load_state("networkidle", timeout=20000)
            png = screenshot_to(ctx.output_dir, page)
            return ApplyResult(
                submitted=True,
                screenshot_path=str(png),
                confirmation_url=page.url,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("submit via %s failed: %s", s, e)
            continue

    return ApplyResult(
        submitted=False,
        barriers=[make_unrecoverable_barrier("submit button not found", url=page.url)],
    )
