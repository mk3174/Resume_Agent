"""Per-portal appliers. Importing the package registers every applier with
`apply.base.get_applier` via side-effect import.
"""

from __future__ import annotations

# Side-effect imports register each applier in the registry.
# Order doesn't matter; register() is idempotent per JobSource.
# linkedin and indeed import patchright lazily, so an environment without the
# `playwright` extras can still import this package.
from apply import ashby, greenhouse, indeed, lever, linkedin, workday  # noqa: F401

from apply.base import (  # re-export
    Applier,
    ApplyContext,
    ApplyResult,
    BrowserSession,
    get_applier,
    register,
)

__all__ = [
    "Applier",
    "ApplyContext",
    "ApplyResult",
    "BrowserSession",
    "get_applier",
    "register",
]
