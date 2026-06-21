"""Query embedder (services layer).

The deterministic, key-free signed feature-hashing embedder — BYTE-IDENTICAL to the knowledge-graph-
service write-side embedder (same blake2b feature-hashing, dim + L2 norm). Identity
matters: a query embedded here must land in the exact same vector space as the chunk embeddings KGS
stored, so cosine similarity is meaningful without any model or API key. If the two ever diverge,
semantic search silently degrades — keep them in lockstep (a shared package is the eventual home).
"""

from __future__ import annotations

import hashlib
import math


class HashingEmbedder:
    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
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
