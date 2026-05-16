"""Account creation flow.

Most ATS portals (Greenhouse, Lever, Ashby) don't require an account to apply
- the form on the public job page is the application. So this module is mainly
infrastructure for company-portal cases (e.g. some Workday tenants, careers
sites that gate behind a login).

Behaviour:
  - If credentials.vault has a Credential for this portal+token, use it.
  - Else attempt a sign-up flow: fill name + email + password (auto-generated
    if not supplied), submit. Hand off to barrier_pause for any CAPTCHA, 2FA,
    or email-verification step.
"""

from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass
from pathlib import Path

from apply.base import detect_ban_signal, safe_fill
from credentials.vault import Credential, get, set_
from notify.telegram import barrier_pause
from orchestrator.state import Barrier, BarrierKind

log = logging.getLogger(__name__)


@dataclass
class SignInResult:
    signed_in: bool
    new_account: bool = False
    barrier: Barrier | None = None


def ensure_signed_in(
    page,
    *,
    portal: str,
    token: str,
    sign_in_url: str | None = None,
    user_email: str,
    user_full_name: str,
) -> SignInResult:
    """Try to sign in (existing creds) or create an account on the current page.

    Returns SignInResult; barriers should be checked by the caller and
    surfaced via Telegram.
    """
    # Try to sign in if the page has a sign-in form.
    cred = get(portal, token)
    if cred:
        if _try_sign_in(page, cred):
            return SignInResult(signed_in=True)

    # Otherwise, attempt account creation.
    new_pw = _gen_password()
    filled = (
        safe_fill(page, "input[name*='name']", user_full_name)
        and safe_fill(page, "input[type='email']", user_email)
        and safe_fill(page, "input[type='password']", new_pw)
    )
    if not filled:
        return SignInResult(
            signed_in=False,
            barrier=Barrier(
                kind=BarrierKind.UNKNOWN,
                message="account creation form not understood",
                context={"portal": portal, "token": token, "url": page.url},
            ),
        )

    submit = page.locator("button[type='submit']").first
    if submit.count() == 0:
        return SignInResult(
            signed_in=False,
            barrier=Barrier(
                kind=BarrierKind.UNKNOWN,
                message="no submit button on account form",
                context={"portal": portal, "token": token},
            ),
        )
    submit.click()
    page.wait_for_load_state("networkidle", timeout=30000)

    ban = detect_ban_signal(page)
    if ban:
        return SignInResult(
            signed_in=False,
            barrier=Barrier(
                kind=BarrierKind.BAN_SIGNAL,
                message=f"ban signal during signup: {ban}",
                context={"portal": portal, "token": token, "url": page.url},
            ),
        )

    if _needs_email_verification(page):
        b = Barrier(
            kind=BarrierKind.EMAIL_VERIFY,
            message=f"{portal}/{token} signup needs email verification",
            context={"email": user_email, "url": page.url},
        )
        # Block on user; expected reply is the OTP code or an "ok" once verified.
        reply = barrier_pause(b)
        b.human_response = reply
        # The caller may have moved on by now; return barrier so they can persist it.
        return SignInResult(signed_in=False, new_account=True, barrier=b)

    set_(portal, token, Credential(username=user_email, password=new_pw))
    return SignInResult(signed_in=True, new_account=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _try_sign_in(page, cred: Credential) -> bool:
    if not safe_fill(page, "input[type='email']", cred.username):
        return False
    if cred.password:
        safe_fill(page, "input[type='password']", cred.password)
    try:
        btn = page.locator("button[type='submit']").first
        if btn.count() == 0:
            return False
        btn.click()
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception as e:  # noqa: BLE001
        log.debug("sign-in submit failed: %s", e)
        return False
    return "/login" not in page.url and "/signin" not in page.url


def _needs_email_verification(page) -> bool:
    body = (page.content() or "").lower()
    return any(k in body for k in (
        "verify your email",
        "check your inbox",
        "verification link",
        "we just sent",
    ))


def _gen_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))
