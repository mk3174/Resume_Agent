"""LLM router.

Reads `config/settings.yaml -> llm` and exposes:
    chat(task, prompt, ...) -> str
    embed(texts) -> list[list[float]]

Tries providers in the configured order for `task`; falls back on ProviderError.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

from .providers.anthropic import AnthropicChat
from .providers.base import ChatProvider, EmbeddingProvider, ProviderError
from .providers.ollama import OllamaChat, OllamaEmbeddings
from .providers.openai import OpenAIChat

log = logging.getLogger(__name__)


def _expand_env(value: Any) -> Any:
    """Tiny YAML-style ${VAR:-default} expander for string values."""
    if not isinstance(value, str):
        return value
    if not value.startswith("${") or not value.endswith("}"):
        return value
    inner = value[2:-1]
    if ":-" in inner:
        var, default = inner.split(":-", 1)
        return os.getenv(var, default)
    return os.getenv(inner, "")


class LLMRouter:
    def __init__(self, llm_config: dict[str, Any]):
        self._cfg = llm_config
        self._providers: dict[str, ChatProvider] = {}
        self._embedder: EmbeddingProvider | None = None

    # ---- lazy provider construction --------------------------------------

    def _provider_cache_key(self, name: str, task: str) -> str:
        if name == "ollama" and task == "rerank":
            return "ollama:rerank"
        return name

    def _get_provider(self, name: str, *, task: str) -> ChatProvider:
        key = self._provider_cache_key(name, task)
        if key in self._providers:
            return self._providers[key]
        pcfg = self._cfg["providers"].get(name, {})
        if name == "ollama":
            host = _expand_env(pcfg.get("host", "http://localhost:11434"))
            models = pcfg.get("models") or {}
            model = models.get("rerank", models.get("chat")) if task == "rerank" else models.get("chat")
            if not model:
                raise ProviderError("ollama models.chat not configured")
            timeout = pcfg.get("timeout_s", 120)
            self._providers[key] = OllamaChat(host=host, model=model, timeout_s=timeout)
        elif name == "anthropic":
            self._providers[key] = AnthropicChat(
                model=pcfg["model"],
                max_tokens=pcfg.get("max_tokens", 4096),
                temperature=pcfg.get("temperature", 0.2),
            )
        elif name == "openai":
            self._providers[key] = OpenAIChat(
                model=pcfg["model"],
                max_tokens=pcfg.get("max_tokens", 4096),
                temperature=pcfg.get("temperature", 0.2),
            )
        else:
            raise ValueError(f"Unknown provider: {name}")
        return self._providers[key]

    def get_embedder(self) -> EmbeddingProvider:
        if self._embedder is not None:
            return self._embedder
        # Embeddings are local-only by default.
        pcfg = self._cfg["providers"]["ollama"]
        host = _expand_env(pcfg.get("host", "http://localhost:11434"))
        model = pcfg["models"]["embed"]
        self._embedder = OllamaEmbeddings(host=host, model=model, timeout_s=pcfg.get("timeout_s", 120))
        return self._embedder

    # ---- public API ------------------------------------------------------

    def chat(
        self,
        task: str,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        think: bool | None = None,
    ) -> tuple[str, str]:
        """Returns (response_text, provider_name_used)."""
        order: list[str] = self._cfg["routes"].get(task, ["ollama"])
        last_err: Exception | None = None
        for prov_name in order:
            try:
                provider = self._get_provider(prov_name, task=task)
            except ProviderError as e:
                log.warning("provider %s unavailable: %s", prov_name, e)
                last_err = e
                continue
            try:
                out = provider.chat(
                    prompt,
                    system=system,
                    json_mode=json_mode,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    think=think,
                )
                if not (out or "").strip():
                    raise ProviderError(
                        f"{prov_name} returned empty response for task={task} "
                        "(common with local models on long JSON prompts; trying next provider)"
                    )
                return out, prov_name
            except ProviderError as e:
                log.warning("provider %s failed for task=%s: %s", prov_name, task, e)
                last_err = e
                continue
        raise ProviderError(
            f"All providers failed for task={task}. Last error: {last_err}"
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.get_embedder().embed(texts)


# ---------------------------------------------------------------------------
# Module-level singleton wired to config/settings.yaml.
# Construct lazily so importing this module is cheap and side-effect-free.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_router() -> LLMRouter:
    from config_loader import load_settings  # local import to avoid circulars

    settings = load_settings()
    return LLMRouter(settings["llm"])
