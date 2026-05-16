"""OpenAI provider. Optional, opt-in via settings.yaml."""

from __future__ import annotations

import os

from .base import ChatProvider, ProviderError


class OpenAIChat(ChatProvider):
    name = "openai"

    def __init__(self, model: str, max_tokens: int = 4096, temperature: float = 0.2):
        try:
            import openai
        except ImportError as e:  # pragma: no cover
            raise ProviderError("openai SDK not installed") from e
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ProviderError("OPENAI_API_KEY not set")
        self._client = openai.OpenAI(api_key=api_key)
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
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=max_tokens or self._max_tokens_default,
                temperature=temperature if temperature is not None else self._temperature_default,
                response_format={"type": "json_object"} if json_mode else None,
            )
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"openai chat failed: {e}") from e
        return resp.choices[0].message.content or ""
