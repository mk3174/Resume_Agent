"""Tiny YAML config loader with .env support and ${VAR:-default} expansion.

Centralised here so every module imports the same source of truth.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = PROJECT_ROOT / "config"

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-(.*?))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2) or ""
            return os.getenv(var, default)
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        # Fall back to .example.yaml so a fresh checkout still works.
        example = path.with_name(path.stem + ".example.yaml")
        if example.exists():
            path = example
        else:
            raise FileNotFoundError(f"Config not found: {path}")
    with path.open("r") as f:
        data = yaml.safe_load(f) or {}
    return _expand(data)


@lru_cache(maxsize=1)
def _ensure_env() -> None:
    """Load .env once. Idempotent."""
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)


@lru_cache(maxsize=1)
def load_settings() -> dict[str, Any]:
    _ensure_env()
    return _load_yaml(CONFIG_DIR / "settings.yaml")


@lru_cache(maxsize=1)
def load_preferences() -> dict[str, Any]:
    _ensure_env()
    return _load_yaml(CONFIG_DIR / "preferences.yaml")


@lru_cache(maxsize=1)
def load_personal_facts() -> dict[str, Any]:
    _ensure_env()
    return _load_yaml(CONFIG_DIR / "personal_facts.yaml")


def reload_all() -> None:
    """Clear caches; useful in tests."""
    load_settings.cache_clear()
    load_preferences.cache_clear()
    load_personal_facts.cache_clear()
    _ensure_env.cache_clear()
