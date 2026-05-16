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


def check_all() -> dict[str, tuple[bool, str]]:
    return {
        "Config": _config(),
        "Ollama": _ollama(),
        "Telegram": _telegram(),
        "Anthropic": _anthropic(),
    }
