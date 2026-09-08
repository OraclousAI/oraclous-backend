"""The shared embedder seam (#643 / #949).

Both `knowledge-graph-service` (write side) and `knowledge-retriever-service` (read side) used to
carry their OWN copy of this: KGS the full seam (`Embedder` Protocol, `HashingEmbedder`,
`OpenAIEmbedder`, `make_embedder`), KRS a lone, hand-duplicated `HashingEmbedder` whose own
docstring said "BYTE-IDENTICAL... keep them in lockstep". #643 exists because that promise was
unenforced — nothing stopped the two copies drifting, and a drift is silent: cosine similarity
between two different vector spaces still returns a plausible-looking score.

This package is the ONE implementation both services import (never re-implement), plus
`embedder_identity`, the ONE function both the write side (stamping `:Chunk.embedder_id`) and the
read side (the semantic Cypher's `embedder_id` filter, #949 Q3) call — so a spelling drift becomes
a merge conflict, not a silent divergence.

Duck-typed settings/credential on purpose: this package sits BELOW both services (`packages/`
never imports a service, and importing one service's concrete `Settings`/credential type from here
would make the other service's use of this package an indirect upward dependency on its sibling —
forbidden, CLAUDE.md §3.1). `Settings` needs only `.embedder` and `.embedding_dim`; `credential`
needs only `.api_key`.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Protocol, runtime_checkable

# The stock OpenAI embedding model both sides default to. Named once here so `embedder_identity`
# and `make_embedder` can never disagree on the model string.
_DEFAULT_OPENAI_MODEL = "text-embedding-3-small"

#: What a chunk with NO recorded `embedder_id` is read as. Such a chunk predates the identity stamp
#: and was written by the original hashing embedder, so it lives in the `hashing:512` space. Three
#: places have to agree on that reading — the read-side identity filter, the re-embed pass's
#: stale-chunk scan, and anything that backfills — so it is spelled once, here, alongside the
#: identity function itself. It stops being true if the hashing dimension is ever changed, which is
#: exactly why it is a named constant with this note rather than a literal at three call sites.
LEGACY_NULL_EMBEDDER_ID = "hashing:512"


#: Provider answers that mean OUR credential is the problem, not the content we sent: 401/403
#: (rejected or revoked) and 429 (quota exhausted). Matched on the stringified error because the
#: provider SDKs and the extraction library both wrap the original exception in their own type, so
#: the status code survives only in the message.
CREDENTIAL_FAULT_MARKERS = (
    "401",
    "403",
    "429",
    "permissiondenied",
    "authenticationerror",
    "invalid_api_key",
    "quota",
    "insufficient_quota",
    "key limit exceeded",
)


def is_credential_failure(error: BaseException) -> bool:
    """True when a model call failed because the CREDENTIAL is bad, not because the input is.

    Shared, because both sides act on the same answer: the write side fails the run rather than
    reporting a plausible entity count, the read side refuses the search rather than 500ing, and
    both drop the cached credential so a rotation heals within one call instead of one TTL. Two
    copies of this list would drift, and a marker missing from one copy is a whole class of
    credential fault silently reclassified as "the content was bad".
    """
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in CREDENTIAL_FAULT_MARKERS)


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbedderCredentialRequired(Exception):
    """`make_embedder` was asked for the `openai` mode with no credential (fail-closed, §3.5).

    Never silently substitutes the hashing embedder under an `openai` label — that is the exact
    #949 Q4 failure mode.
    """


class HashingEmbedder:
    """Deterministic, key-free signed feature-hashing embedder.

    blake2b feature-hashing into a fixed-dim, L2-normalised vector. Pure stdlib, reproducible
    across machines — the CI/dev path needs no API key and no network.
    """

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
    """Real OpenAI-compatible embeddings (constructed only when an API key is present).

    `base_url` is optional so the same client can reach an OpenAI-compatible endpoint (e.g.
    OpenRouter) — when None the openai client uses its own default (api.openai.com).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_OPENAI_MODEL,
        dim: int = 512,
        base_url: str | None = None,
    ) -> None:
        self.dim = dim
        self._model = model
        self._api_key = api_key
        self._base_url = base_url

    def embed(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI

        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        out: list[list[float]] = []
        for start in range(0, len(texts), 256):
            batch = texts[start : start + 256]
            response = client.embeddings.create(model=self._model, input=batch, dimensions=self.dim)
            # Never trust response order — re-sort by .index before zipping back.
            ordered = sorted(response.data, key=lambda d: d.index)
            out.extend([d.embedding for d in ordered])
        return out


def make_embedder(settings: Any, *, credential: Any | None = None) -> Embedder:
    """Build the embedder `settings.embedder` selects.

    `hashing` never needs a credential. `openai` requires one — a model call over customer content
    is billed to the organisation's own credential (#949 Q1), never a platform key, and a missing
    credential REFUSES rather than silently falling back to hashing under an `openai` label.
    """
    if settings.embedder == "openai":
        if credential is None:
            raise EmbedderCredentialRequired(
                "embedding this content needs the organisation's model credential; none was "
                "resolved for this call"
            )
        return OpenAIEmbedder(
            api_key=credential.api_key,
            dim=settings.embedding_dim,
            base_url=getattr(settings, "openai_base_url", None) or None,
        )
    return HashingEmbedder(dim=settings.embedding_dim)


def embedder_identity(settings: Any) -> str:
    """The ONE spelling of "which embedder produced this vector", read by both C3 write (stamping
    `:Chunk.embedder_id`) and C3 read (the semantic Cypher's `embedder_id` filter) — the entire fix
    for #643's "the two embedders can spell their identity differently" failure mode.

    `hashing:<dim>` / `openai:<model>:<dim>`. Pure and side-effect-free (repeatable), which is what
    makes it safe to call from both a Celery worker (write) and a FastAPI request (read).
    """
    if settings.embedder == "openai":
        return f"openai:{_DEFAULT_OPENAI_MODEL}:{settings.embedding_dim}"
    return f"hashing:{settings.embedding_dim}"
