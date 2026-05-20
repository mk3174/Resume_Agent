"""Tiny health checks for the sidebar."""

from __future__ import annotations

import os

import httpx

from config_loader import load_settings


def _ollama() -> tuple[bool, str]:
    cfg = load_settings()["llm"]["providers"]["ollama"]
    host = cfg.get("host") or "http://localhost:11434"
    try:
        r = httpx.get(f"{host}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = [m["name"] for m in r.json().get("models", [])]
        chat = cfg["models"]["chat"]
        embed = cfg["models"]["embed"]
        miss = [m for m in (chat, embed) if not any(n.startswith(m) for n in names)]
        if miss:
            return False, f"missing models: {', '.join(miss)}"
        return True, f"{len(names)} models pulled"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable ({e.__class__.__name__})"


def _config() -> tuple[bool, str]:
    try:
        load_settings()
        return True, "settings.yaml ok"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _telegram() -> tuple[bool, str]:
    if not os.getenv("TELEGRAM_BOT_TOKEN") or not os.getenv("TELEGRAM_CHAT_ID"):
        return False, "no token (console fallback)"
    return True, "configured"


def _anthropic() -> tuple[bool, str]:
    if os.getenv("ANTHROPIC_API_KEY"):
        return True, "key set"
    return False, "no key (local-only)"


def _linkedin_session() -> tuple[bool, str]:
    """Check storage_state shape without echoing cookie values."""
    import json
    from pathlib import Path

    from config_loader import PROJECT_ROOT

    cfg = load_settings()["ingest"].get("linkedin", {}) or {}
    if not cfg.get("enabled", False):
        return True, "ingest disabled (keep off until you need LinkedIn)"

    rel = cfg.get("storage_state_path", "storage_state/linkedin.json")
    p = Path(rel)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    if not p.exists():
        return False, "storage_state missing — run: uv run resume-agent linkedin-login"

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return False, f"invalid JSON ({e})"

    cookies = data.get("cookies") or []
    names = {
        c.get("name")
        for c in cookies
        if isinstance(c, dict) and "linkedin" in (c.get("domain") or "").lower()
    }
    if "li_at" in names:
        return True, "session OK (signed-in cookie present; do not share this file)"
    return False, "file exists but no li_at — re-run linkedin-login"


def check_all() -> dict[str, tuple[bool, str]]:
    return {
        "Config": _config(),
        "Ollama": _ollama(),
        "Telegram": _telegram(),
        "Anthropic": _anthropic(),
        "LinkedIn": _linkedin_session(),
    }
