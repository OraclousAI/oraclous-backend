"""oraclous-embedding — the shared embedder seam (#643 / #949).

`Embedder` protocol + `HashingEmbedder` (deterministic, key-free) + `OpenAIEmbedder` (real,
credential-gated), selected by `make_embedder(settings, *, credential=None)`, plus
`embedder_identity(settings)` — the one function both the write side (knowledge-graph-service) and
the read side (knowledge-retriever-service) call to spell "which embedder produced this vector" so
the two can never drift apart again.
"""

from oraclous_embedding.embedder import (
    CREDENTIAL_FAULT_MARKERS,
    LEGACY_NULL_EMBEDDER_ID,
    Embedder,
    EmbedderCredentialRequired,
    HashingEmbedder,
    OpenAIEmbedder,
    embedder_identity,
    is_credential_failure,
    make_embedder,
)

__all__ = [
    "CREDENTIAL_FAULT_MARKERS",
    "LEGACY_NULL_EMBEDDER_ID",
    "Embedder",
    "EmbedderCredentialRequired",
    "HashingEmbedder",
    "OpenAIEmbedder",
    "embedder_identity",
    "is_credential_failure",
    "make_embedder",
]
