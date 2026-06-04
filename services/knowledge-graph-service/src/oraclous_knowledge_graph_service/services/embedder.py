"""Embedding seam (ORAA-4 §21 services layer).

`Embedder` Protocol with two implementations:
  - HashingEmbedder (DEFAULT, key-free): deterministic signed feature-hashing into a fixed-dim,
    L2-normalised vector. Pure stdlib, reproducible across machines — the CI/dev path needs no API
    key and no network. Good enough for the write side + similarity smoke.
  - OpenAIEmbedder (optional): real embeddings when `KGS_OPENAI_API_KEY` is set. Ports the legacy
    batching (256) and the load-bearing `response.data`-by-`.index` re-sort (never trust order).
Selected by `KGS_EMBEDDER`; the default never imports `openai`.
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol, runtime_checkable

from oraclous_knowledge_graph_service.core.config import Settings


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic, key-free signed feature-hashing embedder."""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            h = int.from_bytes(digest, "big")
            bucket = h % self.dim
            sign = 1.0 if (h >> 16) & 1 else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


class OpenAIEmbedder:
    """Real OpenAI embeddings (optional; constructed only when an API key is present)."""

    def __init__(self, *, api_key: str, model: str = "text-embedding-3-small", dim: int = 512):
        self.dim = dim
        self._model = model
        self._api_key = api_key

    def embed(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI

        client = OpenAI(api_key=self._api_key)
        out: list[list[float]] = []
        for start in range(0, len(texts), 256):
            batch = texts[start : start + 256]
            response = client.embeddings.create(model=self._model, input=batch, dimensions=self.dim)
            # never trust response order — re-sort by .index before zipping back
            ordered = sorted(response.data, key=lambda d: d.index)
            out.extend([d.embedding for d in ordered])
        return out


def make_embedder(settings: Settings) -> Embedder:
    if settings.embedder == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("KGS_EMBEDDER=openai requires KGS_OPENAI_API_KEY")
        return OpenAIEmbedder(api_key=settings.openai_api_key, dim=settings.embedding_dim)
    return HashingEmbedder(dim=settings.embedding_dim)
