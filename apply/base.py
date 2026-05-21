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


def safe_fill(page, selector: str, value: str, *, timeout_ms: int = 8000) -> bool:
    """Best-effort fill: returns True if the field existed and was filled."""
    if not (value or "").strip():
        return False
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout_ms)
        loc.scroll_into_view_if_needed()
        loc.fill(value)
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("safe_fill skipped %s: %s", selector, e)
        return False


# DOM scan used by iter_form_fields / collect_unfilled_required. Lever and
# Greenhouse often use wrapper divs instead of <label for="...">.
_FORM_SCAN_JS = """
() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const labelFor = (el) => {
    const card = el.closest('.application-question, .application-field, li');
    if (card) {
      const q = card.querySelector('.application-label, .application-question, legend, h3, h4, label:not([for])');
      const qText = q ? norm(q.innerText) : '';
      const type = (el.type || '').toLowerCase();
      if (qText && (type === 'checkbox' || type === 'radio')) {
        const sub = norm(el.value || el.getAttribute('aria-label') || '');
        if (sub && sub !== qText) return `${qText} — ${sub}`;
        return qText;
      }
      if (qText) return qText;
    }
    const id = el.id;
    if (id) {
      const lab = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (lab) return norm(lab.innerText);
    }
    const wrap = el.closest('.application-field, .application-question, .field, fieldset, li');
    if (wrap) {
      const lab = wrap.querySelector('.application-label, .application-question, legend, label, h3, h4');
      if (lab) return norm(lab.innerText);
    }
    return norm(el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.value || el.name || el.id || '');
  };
  const isRequired = (el, label) => {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    if (label.includes('*')) return true;
    const wrap = el.closest('.application-field, .application-question, .field');
    if (wrap && wrap.querySelector('.required')) return true;
    return false;
  };
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const st = window.getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  };
  const isEmptyValue = (el, tag, type) => {
    if (type === 'checkbox') return !el.checked;
    if (type === 'radio') {
      const group = document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`);
      return ![...group].some((r) => r.checked);
    }
    if (tag === 'select') {
      const opt = el.options[el.selectedIndex];
      if (!opt) return true;
      const t = norm(opt.text).toLowerCase();
      const v = norm(opt.value);
      if (!v) return true;
      if (t.startsWith('select') || t === 'choose' || t === '--') return true;
      return false;
    }
    return !norm(el.value);
  };

  const fields = [];
  const seenRadio = new Set();
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'file'].includes(type)) continue;
    if (!isVisible(el)) continue;
    const name = el.name || el.id || '';
    if (!name) continue;
    if (type === 'radio') {
      if (seenRadio.has(name)) continue;
      seenRadio.add(name);
    }
    const label = labelFor(el);
    if (!label) continue;
    const selector = el.name
      ? `[name=${JSON.stringify(el.name)}]`
      : `#${CSS.escape(el.id)}`;
    fields.push({
      selector,
      label,
      kind: type === 'radio' ? 'radio' : (type === 'checkbox' ? 'checkbox' : tag),
      name,
      required: isRequired(el, label),
      empty: isEmptyValue(el, tag, type),
    });
  }
  return fields;
}
"""


def scan_form_fields(page) -> list[dict[str, Any]] | None:
    """Return visible form controls with labels, required flag, and empty state.

    Returns None when the DOM scan JS fails (callers must not treat as 'no missing fields').
    """
    try:
        raw = page.evaluate(_FORM_SCAN_JS)
        return raw if isinstance(raw, list) else []
    except Exception as e:  # noqa: BLE001
        log.warning("form scan failed: %s", e)
        return None


def collect_unfilled_required(page) -> list[dict[str, Any]] | None:
    """Required controls that are still empty (pre-submit gate). None if scan failed."""
    fields = scan_form_fields(page)
    if fields is None:
        return None
    return [f for f in fields if f.get("required") and f.get("empty")]


def detect_validation_errors(page) -> list[str]:
    """Visible client-side validation messages after a failed submit attempt."""
    errors: list[str] = []
    for sel in (
        ".error-message",
        ".field-error",
        ".errors",
        "[role='alert']",
        ".application-error",
    ):
        try:
            for el in page.locator(sel).all():
                txt = (el.inner_text() or "").strip()
                if txt and txt not in errors:
                    errors.append(txt)
        except Exception:  # noqa: BLE001
            continue
    return errors


def detect_submit_success(page, url_before: str) -> bool:
    """True when the page looks like a post-submit confirmation, not the form."""
    url = page.url or ""
    if url != url_before:
        low = url.lower()
        if any(token in low for token in ("thank", "confirm", "success", "submitted")):
            return True
        if "lever.co" in low and "/apply" not in low:
            return True

    body = (page.content() or "").lower()
    for phrase in (
        "thank you for applying",
        "application submitted",
        "thanks for applying",
        "we received your application",
        "your application has been received",
    ):
        if phrase in body:
            return True

    still_required = collect_unfilled_required(page)
    if still_required is None or still_required:
        return False
    if detect_validation_errors(page):
        return False

    submit_still = page.locator(
        "button[data-qa='btn-submit'], button:has-text('Submit application'), #submit_app"
    )
    try:
        if submit_still.count() > 0 and submit_still.first.is_visible():
            return False
    except Exception:  # noqa: BLE001
        pass
    return False


def iter_form_fields(page) -> Iterator[tuple[str, str, str]]:
    """Yield (selector, label, kind) for visible empty form controls."""
    fields = scan_form_fields(page)
    if not fields:
        return
    for field in fields:
        if not field.get("empty"):
            continue
        yield field["selector"], field["label"], field["kind"]


def make_unrecoverable_barrier(message: str, **context: Any) -> Barrier:
    return Barrier(kind=BarrierKind.UNKNOWN, message=message, context=context)
