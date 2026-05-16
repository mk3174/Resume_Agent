"""Credential storage.

Per-portal credentials live under a single keyring service `resume-agent`. The
key format is `{portal}:{board_token}`, e.g. `greenhouse:airbnb`. The value
is JSON: {"username": ..., "password": ...}.

For portals where you sign in with email + magic link, set username only and
the apply.account flow will request a magic-link OTP via Telegram.

Falls back to a plain JSON file under data/credentials.json (gitignored) when
keyring is unavailable. NOT encrypted in fallback mode — set up keyring on
your platform for real security.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import keyring
import keyring.errors

from config_loader import PROJECT_ROOT

log = logging.getLogger(__name__)
SERVICE = "resume-agent"
FALLBACK_PATH = PROJECT_ROOT / "data" / "credentials.json"


@dataclass
class Credential:
    username: str
    password: str | None = None  # None for magic-link portals


def _key(portal: str, token: str) -> str:
    return f"{portal}:{token}"


def get(portal: str, token: str) -> Credential | None:
    k = _key(portal, token)
    try:
        raw = keyring.get_password(SERVICE, k)
    except keyring.errors.KeyringError as e:
        log.warning("keyring read failed for %s: %s", k, e)
        raw = _file_get(k)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return Credential(username=d["username"], password=d.get("password"))
    except (json.JSONDecodeError, KeyError) as e:
        log.warning("malformed credential for %s: %s", k, e)
        return None


def set_(portal: str, token: str, cred: Credential) -> None:
    k = _key(portal, token)
    payload = json.dumps({"username": cred.username, "password": cred.password})
    try:
        keyring.set_password(SERVICE, k, payload)
        return
    except keyring.errors.KeyringError as e:
        log.warning("keyring write failed for %s: %s; using fallback file", k, e)
    _file_set(k, payload)


def delete(portal: str, token: str) -> None:
    k = _key(portal, token)
    try:
        keyring.delete_password(SERVICE, k)
    except keyring.errors.KeyringError:
        pass
    _file_delete(k)


# ---------------------------------------------------------------------------
# JSON file fallback (gitignored, NOT encrypted)
# ---------------------------------------------------------------------------


def _file_load() -> dict[str, str]:
    if not FALLBACK_PATH.exists():
        return {}
    try:
        return json.loads(FALLBACK_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _file_save(d: dict[str, str]) -> None:
    FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    FALLBACK_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
    try:
        FALLBACK_PATH.chmod(0o600)
    except OSError:
        pass


def _file_get(k: str) -> str | None:
    return _file_load().get(k)


def _file_set(k: str, v: str) -> None:
    d = _file_load()
    d[k] = v
    _file_save(d)


def _file_delete(k: str) -> None:
    d = _file_load()
    if k in d:
        del d[k]
        _file_save(d)
