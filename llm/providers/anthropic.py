"""Anthropic Claude provider. Optional, opt-in via settings.yaml."""

from __future__ import annotations

import os

from .base import ChatProvider, ProviderError


class AnthropicChat(ChatProvider):
    name = "anthropic"

    def __init__(self, model: str, max_tokens: int = 4096, temperature: float = 0.2):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise ProviderError("anthropic SDK not installed") from e
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens_default = max_tokens
        self._temperature_default = temperature

    def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        think: bool | None = None,  # unused; kept for ChatProvider compatibility
    ) -> str:
        # Anthropic doesn't have a hard JSON mode flag, but we coerce by
        # appending an instruction. Strong enough on Sonnet/Opus.
        prompt_final = prompt
        if json_mode:
            prompt_final += (
                "\n\nReturn ONLY a single valid JSON object. "
                "Do not include prose, code fences, or commentary."
            )
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens or self._max_tokens_default,
                temperature=temperature if temperature is not None else self._temperature_default,
                system=system or "",
                messages=[{"role": "user", "content": prompt_final}],
            )
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"anthropic chat failed: {e}") from e

        # Concatenate all text blocks.
        return "".join(block.text for block in resp.content if getattr(block, "text", None))
