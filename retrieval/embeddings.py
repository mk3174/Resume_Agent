"""Thin wrapper around the LLM router's embedder, plus cosine util."""

from __future__ import annotations

import math

import numpy as np

from llm.router import get_router


def embed(texts: list[str]) -> list[list[float]]:
    return get_router().embed(texts)


def cosine(a: list[float], b: list[float]) -> float:
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0.0 or math.isnan(denom):
        return 0.0
    return float(np.dot(va, vb) / denom)
