"""Ollama provider. Default for local-first runs."""

from __future__ import annotations

import json
from typing import Any

import ollama

from .base import ChatProvider, EmbeddingProvider, ProviderError


class OllamaChat(ChatProvider):
    name = "ollama"

    def __init__(self, host: str, model: str, timeout_s: int = 120):
        self._client = ollama.Client(host=host, timeout=timeout_s)
        self._model = model

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
        # Qwen3-family models default to a "thinking" mode that emits a long
        # <think>...</think> block before the answer. For short structured
        # outputs (rerank IDs, doctor smoke-tests, JSON extraction) thinking
        # blows the num_predict budget and the model returns nothing parseable.
        # We disable thinking by default and let callers opt in explicitly.
        prompt_eff = prompt
        if think is False or (think is None and self._is_qwen3()):
            if "/no_think" not in prompt_eff:
                prompt_eff = f"{prompt_eff}\n\n/no_think"

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt_eff})

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            # Ollama supports `format="json"`. Most modern open models honour it.
            kwargs["format"] = "json"

        try:
            resp = self._client.chat(**kwargs)
        except Exception as e:  # noqa: BLE001 - we re-raise as ProviderError
            raise ProviderError(f"ollama chat failed: {e}") from e

        return resp["message"]["content"]

    def _is_qwen3(self) -> bool:
        m = (self._model or "").lower()
        return m.startswith("qwen3") or "qwen3" in m


class OllamaEmbeddings(EmbeddingProvider):
    name = "ollama-embed"

    def __init__(self, host: str, model: str, timeout_s: int = 120):
        self._client = ollama.Client(host=host, timeout=timeout_s)
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            try:
                resp = self._client.embeddings(model=self._model, prompt=t)
            except Exception as e:  # noqa: BLE001
                raise ProviderError(f"ollama embed failed: {e}") from e
            out.append(list(resp["embedding"]))
        return out
