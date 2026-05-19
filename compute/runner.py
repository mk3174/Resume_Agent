"""Compute Runner protocol (Phase 5).

The agent never calls ``thread.run()`` directly — everything that might want to
move off the laptop later (LLM-heavy steps, batch ingestion, the LangGraph
pipeline itself) is dispatched through a ``Runner``.

Available runners:

- :class:`InProcessRunner` — default; runs the callable on the current event
  loop / thread. Zero-config, fits 1-user-on-a-laptop.
- :class:`OllamaHostRunner` — no-op wrapper that documents the simplest
  Mac-mini offload path: keep the orchestrator local, point ``OLLAMA_HOST`` at
  the mini. All LLM calls automatically run on the mini; the rest stays local.
- :class:`SSHRunner` — runs a small Python snippet on a remote host over SSH.
  Useful when you want to push *non-LLM* batch jobs (e.g. nightly re-ingest)
  to the Mac mini without standing up a Ray cluster. Optional; only imports
  ``paramiko`` lazily.
- :class:`RayRunner` — placeholder for full orchestrator offload via Ray
  actors. Activated via ``settings.compute.mode: ray`` and ``ray`` installed.

Configuration lives under ``settings.compute``::

    compute:
      mode: in_process     # in_process | ollama_host | ssh | ray
      ssh:
        host: mac-mini.local
        user: jk
      ray:
        address: ray://mac-mini.local:10001
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Protocol, TypeVar, runtime_checkable

from config_loader import load_settings

log = logging.getLogger(__name__)

T = TypeVar("T")


@runtime_checkable
class Runner(Protocol):
    """Anything that can execute a zero-arg callable and return its result."""

    name: str

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T: ...


# ---------------------------------------------------------------------------
# In-process (default)
# ---------------------------------------------------------------------------


class InProcessRunner:
    name = "in_process"

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# Ollama-host pattern (LLM-only offload, zero code change)
# ---------------------------------------------------------------------------


class OllamaHostRunner:
    """No-op runner that documents the recommended Mac-mini pattern.

    Setting ``OLLAMA_HOST=http://mac-mini.local:11434`` in the environment is
    sufficient to push every LLM call to the mini while keeping the rest of
    the pipeline local. The runner exists so callers can ``assert isinstance(
    get_runner(), OllamaHostRunner)`` in tests, and so the README has a
    concrete class to point at.
    """

    name = "ollama_host"

    def __init__(self, host: str | None = None) -> None:
        if host:
            os.environ.setdefault("OLLAMA_HOST", host)

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# SSH-to-Mac-mini (non-LLM batch jobs)
# ---------------------------------------------------------------------------


class SSHRunner:
    """Push a callable (serialised as a pickle, executed via `python -c`) to a
    remote host over SSH. Intentionally minimal: not a job queue, just a
    "shell out and wait" primitive for nightly ingestion / heavy embedding
    backfills.
    """

    name = "ssh"

    def __init__(self, host: str, user: str, project_root: str = "/Users/USER/Resume_agent"):
        self.host = host
        self.user = user
        self.project_root = project_root

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        try:
            import base64
            import pickle
            import subprocess

            payload = base64.b64encode(pickle.dumps((fn, args, kwargs))).decode()
            script = (
                "import base64,pickle,sys;"
                f"obj=pickle.loads(base64.b64decode('{payload}'));"
                "fn,args,kw=obj;print(repr(fn(*args,**kw)))"
            )
            cmd = [
                "ssh",
                f"{self.user}@{self.host}",
                f"cd {self.project_root} && uv run python -c \"{script}\"",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return eval(res.stdout.strip())  # noqa: S307 - trusted host only
        except Exception as e:  # noqa: BLE001
            log.error("SSHRunner failed: %s; falling back in-process", e)
            return fn(*args, **kwargs)


# ---------------------------------------------------------------------------
# Ray actor (full offload — opt-in)
# ---------------------------------------------------------------------------


class RayRunner:
    name = "ray"

    def __init__(self, address: str | None = None):
        self.address = address
        self._init = False

    def _ensure_init(self) -> None:
        if self._init:
            return
        try:
            import ray  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("ray not installed; `pip install ray[default]`") from e
        import ray

        if not ray.is_initialized():
            ray.init(address=self.address) if self.address else ray.init()
        self._init = True

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        self._ensure_init()
        import ray

        return ray.get(ray.remote(fn).remote(*args, **kwargs))


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def get_runner() -> Runner:
    cfg = (load_settings().get("compute") or {})
    mode = (cfg.get("mode") or os.getenv("RUNNER", "in_process")).strip().lower()
    if mode == "ollama_host":
        return OllamaHostRunner(host=cfg.get("ollama_host"))
    if mode == "ssh":
        ssh = cfg.get("ssh", {}) or {}
        return SSHRunner(
            host=ssh.get("host", "localhost"),
            user=ssh.get("user", os.getenv("USER", "root")),
            project_root=ssh.get("project_root", "/Users/USER/Resume_agent"),
        )
    if mode == "ray":
        ray_cfg = cfg.get("ray", {}) or {}
        return RayRunner(address=ray_cfg.get("address"))
    return InProcessRunner()
