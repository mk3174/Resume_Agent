"""Provider Protocol. Every LLM/embedding provider implements this."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ChatProvider(Protocol):
    """Sync chat interface. Sync because the orchestrator already uses asyncio
    `run_in_executor` for IO-bound LLM calls; sync provider impls are simpler
    to reason about and keep cancellation predictable.
    """

    name: str

    def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        think: bool | None = None,
    ) -> str:
        """Return the assistant message text. Raises on transport failure.

        `think` is only honoured by providers serving reasoning-capable models
        (e.g. Qwen3 via Ollama, which supports `/no_think`). Other providers ignore it.
        """
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text."""
        ...


class ProviderError(RuntimeError):
    """Raised when a provider fails in a way the router should consider for fallback."""


class BudgetExceeded(RuntimeError):
    """Raised when a paid provider's monthly budget cap has been hit."""
