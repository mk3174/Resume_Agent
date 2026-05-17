"""Ollama provider. Default for local-first runs."""

from __future__ import annotations

import json
from typing import Any

import ollama

from .base import ChatProvider, EmbeddingProvider, ProviderError


class OllamaChat(ChatProvider):
    name = "ollama"

    def __init__(self, host: str, model: str, timeout_s: int = 120):
        self._host = host
        self._timeout_s = timeout_s
        self._model = model
        self._client = ollama.Client(host=host, timeout=timeout_s)

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

        # Qwen3 defaults to "thinking" mode: the model writes to `message.thinking`
        # and leaves `message.content` empty unless we pass think=False on the API.
        # Prompt suffix /no_think alone is not enough (see ollama/ollama#12234).
        if think is True:
            kwargs["think"] = True
        elif think is False or (think is None and self._is_qwen3()):
            kwargs["think"] = False

        # STAR/tailor calls with large num_predict can exceed 10 min on CPU; scale timeout.
        call_timeout = max(self._timeout_s, min(3600, max_tokens + 180))
        client = self._client if call_timeout <= self._timeout_s else ollama.Client(
            host=self._host, timeout=call_timeout
        )

        try:
            resp = client.chat(**kwargs)
        except Exception as e:  # noqa: BLE001 - we re-raise as ProviderError
            raise ProviderError(f"ollama chat failed: {e}") from e

        msg = resp["message"]
        content = (msg.get("content") if isinstance(msg, dict) else msg.content) or ""
        if not str(content).strip():
            thinking = msg.get("thinking") if isinstance(msg, dict) else getattr(msg, "thinking", None)
            if thinking:
                content = thinking
        return str(content)

    def _is_qwen3(self) -> bool:
        m = (self._model or "").lower()
        return m.startswith("qwen3") or "qwen3" in m


class OllamaEmbeddings(EmbeddingProvider):
    name = "ollama-embed"

    def __init__(self, host: str, model: str, timeout_s: int = 120):
        self._client = ollama.Client(host=host, timeout=timeout_s)
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Ollama accepts batched `input`; one request per chunk is far faster than
        # sequential single-text calls (filtering ~1k jobs used to take 10+ minutes).
        batch_size = 64
        out: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            chunk = texts[i : i + batch_size]
            try:
                resp = self._client.embed(model=self._model, input=chunk)
            except Exception as e:  # noqa: BLE001
                raise ProviderError(f"ollama embed failed: {e}") from e
            vectors = getattr(resp, "embeddings", None) or resp.get("embeddings")  # type: ignore[union-attr]
            if vectors is None:
                raise ProviderError("ollama embed returned no embeddings")
            out.extend([list(v) for v in vectors])
        return out
