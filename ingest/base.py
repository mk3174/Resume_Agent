"""Ingestor Protocol. Every job source implements this."""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from orchestrator.state import JobPosting


@runtime_checkable
class Ingestor(Protocol):
    """Synchronous because httpx.get is fast and we want simple back-pressure."""

    source_name: str

    def fetch(self, board_token: str) -> Iterable[JobPosting]:
        ...
