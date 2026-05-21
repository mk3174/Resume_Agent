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
import sys
from typing import Any, Iterable

import re

from apply.base import (
    ApplyContext,
    ApplyResult,
    collect_unfilled_required,
    detect_ban_signal,
    detect_submit_success,
    detect_validation_errors,
    iter_form_fields,
    make_unrecoverable_barrier,
    scan_form_fields,
    safe_fill,
    screenshot_to,
)
from config_loader import load_personal_facts
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
    "full_name": ["[name='name']"],
    "email": ["[name='email']", "input[type='email']"],
    "phone": ["[name='phone']", "input[type='tel']"],
    "linkedin": ["[name*='linkedin']", "input[id*='linkedin']", "[name='urls[LinkedIn]']"],
    "github": ["[name*='github']", "input[id*='github']", "[name='urls[GitHub]']"],
    "portfolio": ["[name*='website']", "[name*='portfolio']", "[name='urls[Other]']"],
}

_SKIP_ANSWER_LABELS = (
    "resume",
    "cv",
)

_LANGUAGE_LABEL_RE = re.compile(r"^[A-Za-z].*\([A-Z]{2,5}\)$")


def contact_values(ctx: ApplyContext) -> dict[str, str]:
    """Master resume contact with personal_facts identity as fallback."""
    contact = dict(ctx.master.contact or {})
    identity = load_personal_facts().get("identity") or {}
    fallbacks = {
        "email": identity.get("email"),
        "phone": identity.get("phone"),
        "linkedin": identity.get("linkedin_url"),
        "github": identity.get("github_url"),
        "portfolio": identity.get("portfolio_url"),
    }
    for key, val in fallbacks.items():
        if val and not contact.get(key):
            contact[key] = str(val)
    return contact


def master_name(ctx: ApplyContext) -> str:
    if ctx.master.name.strip():
        return ctx.master.name.strip()
    return str(load_personal_facts().get("identity", {}).get("full_name") or "").strip()


def current_company(ctx: ApplyContext) -> str:
    if ctx.master.experience:
        return ctx.master.experience[0].company
    return ""


def fill_common(page, ctx: ApplyContext) -> None:
    """Fill name/email/phone/links from MasterResume + personal_facts."""
    contact = contact_values(ctx)
    name_parts = master_name(ctx).split(" ", 1)
    first = name_parts[0]
    last = name_parts[1] if len(name_parts) > 1 else ""

    values = {
        "full_name": master_name(ctx),
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


def fill_lever_basics(page, ctx: ApplyContext) -> None:
    """Lever-specific single-page fields (combined name, bracketed URLs)."""
    page.evaluate("window.scrollTo(0, 0)")
    contact = contact_values(ctx)
    safe_fill(page, "[name='name']", master_name(ctx))
    safe_fill(page, "[name='email']", contact.get("email", ""))
    safe_fill(page, "[name='phone']", contact.get("phone", ""))
    org = current_company(ctx)
    if org:
        safe_fill(page, "[name='org']", org)
    for key, val in (
        ("LinkedIn", contact.get("linkedin")),
        ("GitHub", contact.get("github")),
        ("Portfolio", contact.get("portfolio")),
        ("Other", contact.get("portfolio")),
    ):
        if val:
            safe_fill(page, f"[name='urls[{key}]']", val)
    fill_common(page, ctx)


def fill_lever_standard_fields(page, ctx: ApplyContext) -> None:
    """Auto-fill Lever fields that should not go through the LLM Q&A loop."""
    contact = contact_values(ctx)
    portfolio = contact.get("portfolio") or contact.get("github") or ""
    if portfolio:
        safe_fill(page, "[name='urls[Portfolio]']", portfolio)

    location = contact.get("location") or "United States"
    _fill_lever_location(page, location)

    # Language proficiency — English only unless facts say otherwise later.
    try:
        eng = page.locator("input[type='checkbox'][value='English (ENG)']").first
        if eng.count() > 0 and not eng.is_checked():
            eng.scroll_into_view_if_needed()
            eng.check()
    except Exception as e:  # noqa: BLE001
        log.debug("English language checkbox: %s", e)

    dem = load_personal_facts().get("demographics") or {}
    eeo_map = {
        "eeo[gender]": _eeo_label("gender", dem.get("gender")),
        "eeo[race]": _eeo_label("ethnicity", dem.get("ethnicity")),
        "eeo[veteran]": _eeo_label("veteran", dem.get("veteran_status")),
        "eeo[disability]": _eeo_label("disability", dem.get("disability_status")),
    }
    for name, label in eeo_map.items():
        if not label:
            continue
        try:
            sel = page.locator(f"select[name='{name}']").first
            if sel.count() == 0:
                continue
            sel.scroll_into_view_if_needed()
            try:
                sel.select_option(label=label)
            except Exception:  # noqa: BLE001
                sel.select_option(value=label)
        except Exception as e:  # noqa: BLE001
            log.debug("eeo %s: %s", name, e)

    # GDPR / data consent radios
    for phrase in ("Yes, I consent", "I agree", "Yes"):
        try:
            loc = page.locator(f"label:has-text('{phrase}')").first
            if loc.count() > 0 and loc.is_visible():
                loc.scroll_into_view_if_needed()
                loc.click(timeout=3000)
                break
        except Exception:  # noqa: BLE001
            continue


def _eeo_label(kind: str, raw: Any) -> str:
    if raw is None:
        return ""
    val = str(raw).strip()
    if not val or "prefer_not" in val.lower():
        return "Decline to self-identify"
    if kind == "veteran" and "not" in val.lower():
        return "I am not a veteran"
    if kind == "disability" and val.lower() in {"no", "false", "0"}:
        return "No, I don't have a disability"
    if kind == "gender":
        return val.capitalize() if val.lower() in {"male", "female"} else val.replace("_", " ")
    if kind == "ethnicity":
        return val.replace("_", " ").title()
    return val.replace("_", " ")


def _select_option_texts(page, selector: str) -> list[str]:
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return []
        raw = loc.evaluate(
            "el => [...el.options].map(o => (o.text || '').trim()).filter(t => t && !/^(select|choose|--)/i.test(t))"
        )
        return [t for t in (raw or []) if t]
    except Exception:  # noqa: BLE001
        return []


def _pick_select_option(options: list[str], *candidates: str) -> str:
    for cand in candidates:
        c = cand.lower()
        for o in options:
            if c == o.lower() or c in o.lower():
                return o
    return options[0] if options else ""


def _fill_lever_location(page, location_text: str) -> None:
    """Lever location is often a typeahead, not a plain text input."""
    loc = page.locator("[name='location']").first
    if loc.count() == 0:
        return
    try:
        loc.scroll_into_view_if_needed()
        loc.click()
        loc.fill("")
        query = location_text.split("(")[0].strip() or "United States"
        loc.type(query, delay=30)
        page.wait_for_timeout(600)
        for sel in (
            "[role='option']",
            ".dropdown-location-result",
            ".location-result",
            "li.location",
        ):
            opt = page.locator(sel).first
            if opt.count() > 0 and opt.is_visible():
                opt.click()
                return
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
    except Exception as e:  # noqa: BLE001
        log.debug("location typeahead: %s", e)
        safe_fill(page, "[name='location']", location_text)


def _fill_lever_card_fields(page, ctx: ApplyContext) -> None:
    """Fill Lever custom card selects/textareas when field names are opaque."""
    fields = scan_form_fields(page)
    if not fields:
        return
    facts = load_personal_facts()
    wa = facts.get("work_authorization") or {}
    us_ok = bool(wa.get("citizen_or_pr_in"))

    for field in fields:
        if not field.get("empty"):
            continue
        sel = field.get("selector") or ""
        kind = field.get("kind") or "input"
        label = field.get("label") or ""
        name = field.get("name") or ""

        if kind == "select" and (name.startswith("cards[") or label.startswith("cards[")):
            options = _select_option_texts(page, sel)
            if not options:
                continue
            pick = ""
            if us_ok:
                pick = _pick_select_option(
                    options,
                    "yes",
                    "authorized",
                    "no sponsorship",
                    "do not require",
                    "citizen",
                    "united states",
                )
            if not pick:
                pick = _pick_select_option(options, "no", "decline", "prefer not")
            if pick:
                _fill_form_field(page, sel, "select", pick)
            continue

        if kind == "textarea" and (name.startswith("cards[") or label.startswith("cards[")):
            prompt_label = label if not label.startswith("cards[") else f"Custom question for {ctx.job.title} at {ctx.job.company}"
            entry = responder_mod.answer(
                prompt_label,
                job_title=ctx.job.title,
                company=ctx.job.company,
                master=ctx.master,
                tailored=ctx.tailored,
                job_description=ctx.job.description_text or "",
            )
            if entry.answer:
                _fill_form_field(page, sel, kind, entry.answer)
            continue

        if kind == "radio" and field.get("required"):
            name = field.get("name") or ""
            if not name:
                continue
            try:
                yes = page.locator(f"input[type='radio'][name='{name}'][value='Yes']").first
                if yes.count() > 0:
                    yes.scroll_into_view_if_needed()
                    yes.click()
                    continue
                page.locator(f"input[type='radio'][name='{name}']").first.click()
            except Exception as e:  # noqa: BLE001
                log.debug("radio %s: %s", name, e)


def fill_lever_card_fields(page, ctx: ApplyContext) -> None:
    _fill_lever_card_fields(page, ctx)


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
            loc.scroll_into_view_if_needed()
            loc.set_input_files(str(ctx.resume_pdf))
            log.info("uploaded resume via %s", s)
            return True
        except Exception as e:  # noqa: BLE001
            log.debug("upload via %s failed: %s", s, e)
            continue
    return False


def _should_skip_question(label: str) -> bool:
    low = label.lower()
    if any(token in low for token in _SKIP_ANSWER_LABELS):
        return True
    if _LANGUAGE_LABEL_RE.match(label.strip()):
        return True
    if "choose not to disclose" in low:
        return True
    if low.startswith("eeo[") or "eeo[" in low:
        return True
    if "consent" in low and ("yes" in low or "agree" in low):
        return True
    return False


def _fill_form_field(page, selector: str, kind: str, answer: str) -> bool:
    """Fill a single control (text, select, radio, checkbox)."""
    ans = answer.strip()
    if not ans:
        return False
    try:
        loc = page.locator(selector).first
        loc.scroll_into_view_if_needed()
        if kind == "select":
            try:
                loc.select_option(label=ans)
            except Exception:  # noqa: BLE001
                loc.select_option(value=ans)
            return True
        if kind == "radio":
            name = loc.get_attribute("name") or ""
            short = ans
            if len(ans) > 80:
                for token in ("Yes, I consent", "Yes", "No", "Strongly agree", "Agree", "Disagree"):
                    if token.lower() in ans.lower():
                        short = token
                        break
            if name:
                group = page.locator(f"input[type='radio'][name='{name}']")
                for i in range(group.count()):
                    opt = group.nth(i)
                    val = (opt.get_attribute("value") or "").strip()
                    opt_id = opt.get_attribute("id") or ""
                    label_txt = ""
                    if opt_id:
                        lab = page.locator(f"label[for='{opt_id}']").first
                        if lab.count() > 0:
                            label_txt = (lab.inner_text() or "").strip()
                    if short.lower() in {val.lower(), label_txt.lower()}:
                        opt.click()
                        return True
                    if label_txt and short.lower() in label_txt.lower():
                        opt.click()
                        return True
            page.locator(f"label:has-text('{short}')").first.click(timeout=3000)
            return True
        if kind == "checkbox":
            low = ans.lower()
            if low in {"yes", "true", "1", "checked"}:
                loc.check()
                return True
            return False
        loc.fill(ans)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("could not fill %s (%s): %s", selector, kind, e)
        return False


def answer_custom_questions(page, ctx: ApplyContext) -> tuple[list[QAEntry], list[Barrier]]:
    """Walk visible form labels, answer each via Responder, fill or barrier-park."""
    qa: list[QAEntry] = []
    barriers: list[Barrier] = []

    for sel, label, tag in iter_form_fields(page):
        if _should_skip_question(label):
            continue

        entry = responder_mod.answer(
            label,
            job_title=ctx.job.title,
            company=ctx.job.company,
            master=ctx.master,
            tailored=ctx.tailored,
            job_description=ctx.job.description_text or "",
        )
        qa.append(entry)
        if not entry.answer:
            b = Barrier(
                kind=BarrierKind.MISSING_FACT,
                message=f"Need answer for: {label}",
                context={"selector": sel, "tag": tag, "url": page.url},
            )
            if sys.stdin.isatty():
                human = barrier_pause(b, options=["skip"])
                if human.strip():
                    entry = entry.model_copy(update={"answer": human.strip(), "needs_review": False, "source": "human"})
                    qa[-1] = entry
                    responder_mod.remember(entry)
                else:
                    barriers.append(b)
                    continue
            else:
                barriers.append(b)
                continue
        elif entry.needs_review and sys.stdin.isatty():
            b = Barrier(
                kind=BarrierKind.LOW_CONF_QA,
                message=f"Review answer for: {label}",
                context={"selector": sel, "tag": tag, "url": page.url, "draft": entry.answer[:240]},
            )
            human = barrier_pause(b, options=["accept", "skip"])
            if human.strip().lower() not in {"", "skip"}:
                entry = entry.model_copy(
                    update={"answer": human.strip(), "needs_review": False, "source": "human"}
                )
                qa[-1] = entry
                responder_mod.remember(entry)
        if entry.answer:
            _fill_form_field(page, sel, tag, entry.answer)

    return qa, barriers


def _has_blocking_barriers(barriers: list[Barrier]) -> bool:
    return any(
        b.kind in (BarrierKind.LOW_CONF_QA, BarrierKind.MISSING_FACT, BarrierKind.CAPTCHA, BarrierKind.TWO_FACTOR)
        for b in barriers
    )


def prepare_form_before_submit(page, ctx: ApplyContext) -> tuple[list[QAEntry], list[Barrier]]:
    """Scroll the form, answer custom questions; submit_and_screenshot preflights required fields."""
    scroll_form(page)
    return answer_custom_questions(page, ctx)


def scroll_form(page) -> None:
    """Scroll through long single-page forms so lazy sections become visible."""
    try:
        height = int(page.evaluate("() => document.body.scrollHeight") or 0)
        for y in range(0, height, 500):
            page.evaluate(f"window.scrollTo(0, {y})")
            page.wait_for_timeout(120)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(200)
    except Exception as e:  # noqa: BLE001
        log.debug("scroll_form skipped: %s", e)


def submit_and_screenshot(
    page,
    ctx: ApplyContext,
    *,
    submit_selectors: Iterable[str] | None = None,
) -> ApplyResult:
    """Validate the form, submit only when ready, verify success afterward."""
    if ctx.dry_apply:
        png = screenshot_to(ctx.output_dir, page)
        return ApplyResult(
            submitted=False,
            screenshot_path=str(png),
            confirmation_url=page.url,
        )

    missing = collect_unfilled_required(page)
    if missing is None:
        png = screenshot_to(ctx.output_dir, page)
        return ApplyResult(
            submitted=False,
            screenshot_path=str(png),
            confirmation_url=page.url,
            barriers=[
                Barrier(
                    kind=BarrierKind.MISSING_FACT,
                    message="Cannot submit — form field scan failed (page may not have loaded fully)",
                    context={"url": page.url},
                )
            ],
        )
    if missing:
        labels = [m.get("label", m.get("name", "field")) for m in missing[:12]]
        png = screenshot_to(ctx.output_dir, page)
        return ApplyResult(
            submitted=False,
            screenshot_path=str(png),
            confirmation_url=page.url,
            barriers=[
                Barrier(
                    kind=BarrierKind.MISSING_FACT,
                    message="Cannot submit — required fields still empty: " + "; ".join(labels),
                    context={"url": page.url, "missing": labels},
                )
            ],
        )

    ban = detect_ban_signal(page)
    if ban:
        return ApplyResult(
            submitted=False,
            barriers=[Barrier(kind=BarrierKind.BAN_SIGNAL, message=f"ban signal: {ban}",
                              context={"url": page.url})],
        )

    sels = list(submit_selectors or [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Submit')",
        "button:has-text('Apply')",
    ])

    url_before = page.url
    clicked = False
    for s in sels:
        try:
            btn = page.locator(s).first
            if btn.count() == 0:
                continue
            btn.scroll_into_view_if_needed()
            btn.click()
            clicked = True
            page.wait_for_load_state("networkidle", timeout=20000)
            break
        except Exception as e:  # noqa: BLE001
            log.debug("submit via %s failed: %s", s, e)
            continue

    if not clicked:
        return ApplyResult(
            submitted=False,
            barriers=[make_unrecoverable_barrier("submit button not found", url=page.url)],
        )

    png = screenshot_to(ctx.output_dir, page)
    if detect_submit_success(page, url_before):
        return ApplyResult(
            submitted=True,
            screenshot_path=str(png),
            confirmation_url=page.url,
        )

    errors = detect_validation_errors(page)
    still_missing = collect_unfilled_required(page)
    detail_parts: list[str] = []
    if still_missing:
        detail_parts.extend(m.get("label", m.get("name", "field")) for m in still_missing[:8])
    elif still_missing is None:
        detail_parts.append("form field scan failed")
    if errors:
        detail_parts.extend(errors[:3])
    detail = "; ".join(detail_parts) or "form still visible after submit click"
    return ApplyResult(
        submitted=False,
        screenshot_path=str(png),
        confirmation_url=page.url,
        barriers=[
            Barrier(
                kind=BarrierKind.MISSING_FACT,
                message=f"Submit did not complete — {detail}",
                context={"url": page.url},
            )
        ],
    )
