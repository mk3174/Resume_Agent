"""Applier protocol + Patchright session helper.

Each per-portal applier (greenhouse.py, lever.py, ashby.py, workday.py,
linkedin.py, indeed.py) implements `Applier`. The orchestrator picks the
right one off `JobPosting.source` via the `get_applier()` registry below.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

from orchestrator.state import (
    Barrier,
    BarrierKind,
    JobPosting,
    JobSource,
    MasterResume,
    QAEntry,
    TailoredResume,
)

log = logging.getLogger(__name__)

_PATCHRIGHT_INSTALL_HINT = (
    "patchright not installed. Run: uv sync --extra playwright && "
    "uv run patchright install chromium"
)


def patchright_available() -> bool:
    """True when the optional `playwright` extra (Patchright) is installed."""
    import importlib.util

    return importlib.util.find_spec("patchright") is not None


# ---------------------------------------------------------------------------
# Result + context
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    submitted: bool
    confirmation_url: str | None = None
    screenshot_path: str | None = None
    qa: list[QAEntry] = field(default_factory=list)
    barriers: list[Barrier] = field(default_factory=list)
    error: str | None = None


@dataclass
class ApplyContext:
    job: JobPosting
    master: MasterResume
    tailored: TailoredResume
    resume_pdf: Path
    cover_md: Path
    output_dir: Path
    dry_apply: bool = True


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Applier(Protocol):
    source: JobSource

    def apply(self, ctx: ApplyContext) -> ApplyResult:
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_REGISTRY: dict[JobSource, Applier] = {}


def register(applier: Applier) -> None:
    _REGISTRY[applier.source] = applier


def get_applier(source: JobSource) -> Applier | None:
    return _REGISTRY.get(source)


# ---------------------------------------------------------------------------
# Patchright session helper
# ---------------------------------------------------------------------------


class BrowserSession:
    """Context manager around Patchright. Persists storage_state per source."""

    def __init__(
        self,
        *,
        source: JobSource,
        headed: bool = True,
        proxy: str | None = None,
        storage_state_dir: Path | None = None,
    ):
        self.source = source
        self.headed = headed
        self.proxy = proxy
        self.storage_state_dir = storage_state_dir or (Path.cwd() / "storage_state")
        self._pw = None
        self._browser = None
        self._context = None

    def __enter__(self):
        # Lazy import — patchright is optional [extras]
        if not patchright_available():
            raise ModuleNotFoundError(_PATCHRIGHT_INSTALL_HINT)
        from patchright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {"headless": not self.headed}
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}
        self._browser = self._pw.chromium.launch(**launch_kwargs)

        ctx_kwargs: dict[str, Any] = {"viewport": {"width": 1366, "height": 900}}
        state_path = self.storage_state_dir / f"{self.source.value}.json"
        if state_path.exists():
            ctx_kwargs["storage_state"] = str(state_path)
        self._context = self._browser.new_context(**ctx_kwargs)
        return self._context

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._context is not None:
                # Persist storage_state for next time (cookies, localStorage, etc.)
                self.storage_state_dir.mkdir(parents=True, exist_ok=True)
                self._context.storage_state(path=str(self.storage_state_dir / f"{self.source.value}.json"))
                self._context.close()
        finally:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()


# ---------------------------------------------------------------------------
# Common helpers used by per-portal appliers
# ---------------------------------------------------------------------------


def screenshot_to(out_dir: Path, page) -> Path:
    """Save a full-page screenshot under the output dir; returns the path."""
    p = out_dir / "submission_screenshot.png"
    page.screenshot(path=str(p), full_page=True)
    return p


def detect_ban_signal(page) -> str | None:
    """Heuristic: look for common 'we noticed unusual activity' / 429 / blocked pages."""
    body_lower = (page.content() or "").lower()
    for needle in (
        "unusual activity",
        "you've been rate-limited",
        "are you a robot",
        "verify you are human",
        "access denied",
        "403 forbidden",
    ):
        if needle in body_lower:
            return needle
    return None


def safe_fill(page, selector: str, value: str, *, timeout_ms: int = 5000) -> bool:
    """Best-effort fill: returns True if the field existed and was filled."""
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout_ms)
        loc.fill(value)
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("safe_fill skipped %s: %s", selector, e)
        return False


def iter_form_fields(page) -> Iterator[tuple[str, str, str]]:
    """Yield (selector, label, kind) for every visible form input.

    Useful for the Responder loop: feed each unknown label into responder.answer().
    """
    for el in page.locator("input, textarea, select").all():
        try:
            tag = el.evaluate("e => e.tagName.toLowerCase()")
            type_ = el.get_attribute("type") or ""
            if type_ in {"hidden", "submit", "button", "file"}:
                continue
            name = el.get_attribute("name") or el.get_attribute("id") or ""
            label = _label_for(page, name)
            sel = f"[name='{name}']" if name else None
            if sel and label:
                yield sel, label, tag
        except Exception:  # noqa: BLE001
            continue


def _label_for(page, name: str) -> str | None:
    if not name:
        return None
    try:
        loc = page.locator(f"label[for='{name}']").first
        if loc.count() > 0:
            return (loc.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def make_unrecoverable_barrier(message: str, **context: Any) -> Barrier:
    return Barrier(kind=BarrierKind.UNKNOWN, message=message, context=context)
