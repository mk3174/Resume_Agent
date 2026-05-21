"""Telegram notification primitives.

Two surfaces:
  - notify(text):                        fire-and-forget. Used for clean-submit
                                         pings, estimate FYIs, daily summaries.
  - barrier_pause(barrier, ...):         BLOCKS until the user replies in Telegram.
                                         Used for CAPTCHAs, low-conf Q&A, missing
                                         personal facts, ban-detection signals.

In Phase 0/1 we ship a CONSOLE-ONLY fallback so the pipeline runs without a
Telegram bot. Set settings.notify.channel = telegram and provide a token+chat_id
to switch on the real thing.

The python-telegram-bot integration is intentionally optional — it's listed
under `pip install resume-agent[notify]`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from config_loader import load_settings
from orchestrator.state import Barrier

log = logging.getLogger(__name__)


def _channel() -> str:
    return load_settings().get("notify", {}).get("channel", "console")


# ---------------------------------------------------------------------------
# Fire-and-forget
# ---------------------------------------------------------------------------


def notify(text: str, *, level: str = "info", context: dict[str, Any] | None = None) -> None:
    ch = _channel()
    if ch == "telegram":
        _telegram_send(text)
    else:
        log.info("[notify:%s] %s", level, text)
        if context:
            log.debug("context=%s", context)


# ---------------------------------------------------------------------------
# Blocking barrier
# ---------------------------------------------------------------------------


def barrier_pause(
    barrier: Barrier,
    *,
    timeout_s: int | None = None,
    options: list[str] | None = None,
) -> str:
    """Send a barrier ping and BLOCK until human responds.

    In console mode this prompts on stdin. In telegram mode this would
    long-poll for a reply. Returns the human's text response.
    """
    text = (
        f"Barrier hit: {barrier.kind.value}\n"
        f"  message: {barrier.message}\n"
        f"  context: {barrier.context}"
    )
    ch = _channel()
    if ch == "telegram":
        return _telegram_pause(text, options=options, timeout_s=timeout_s)

    print("\n" + "=" * 60, flush=True)
    print(text, flush=True)
    if options:
        for i, o in enumerate(options, 1):
            print(f"  {i}) {o}", flush=True)
    print("Type a response (or just press Enter to skip):", flush=True)
    try:
        return input("> ").strip()
    except EOFError:
        return ""


# ---------------------------------------------------------------------------
# Telegram lazy backend
# ---------------------------------------------------------------------------


def _telegram_send(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("Telegram disabled (missing TELEGRAM_BOT_TOKEN or _CHAT_ID); falling back to console")
        log.info("[notify] %s", text)
        return
    try:
        import httpx  # already a dep

        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]},
            timeout=10.0,
        )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            log.warning(
                "telegram send failed (%s): %s — check TELEGRAM_CHAT_ID and that you /start the bot",
                resp.status_code,
                detail,
            )
            log.info("[notify] %s", text)
    except Exception as e:  # noqa: BLE001
        log.warning("telegram send failed: %s", e)
        log.info("[notify] %s", text)


def _telegram_pause(
    text: str, *, options: list[str] | None, timeout_s: int | None
) -> str:
    """Real implementation lives in Phase 0+ once python-telegram-bot is installed."""
    log.warning(
        "telegram barrier requested but full bot not yet wired; falling back to console"
    )
    print("\n" + "=" * 60, flush=True)
    print(text, flush=True)
    if options:
        for i, o in enumerate(options, 1):
            print(f"  {i}) {o}", flush=True)
    print("Type a response (or Enter to skip):", flush=True)
    try:
        return input("> ").strip()
    except EOFError:
        return ""
