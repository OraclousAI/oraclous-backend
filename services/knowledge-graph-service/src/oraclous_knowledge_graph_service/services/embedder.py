"""Embedding seam (services layer) — thin KGS wiring over the SHARED `oraclous_embedding` package.

#643: this service used to carry its OWN copy of `Embedder`/`HashingEmbedder`/`OpenAIEmbedder`, and
knowledge-retriever-service carried a second, hand-duplicated copy of `HashingEmbedder` — nothing
enforced the two stayed byte-identical. Both classes are now IMPORTED (not re-implemented) from
`packages/embedding` (`oraclous_embedding`), the one shared implementation both services build
their embedder from — a spelling drift becomes a merge conflict, not a silent divergence.

`make_embedder` stays defined here (not re-exported) because it must keep raising THIS service's
own `ModelCredentialUnavailable` (matching every other #724 call site's refusal shape), not the
shared package's generic `EmbedderCredentialRequired`.
"""

from __future__ import annotations

from oraclous_embedding import Embedder, HashingEmbedder, OpenAIEmbedder
from oraclous_embedding import embedder_identity as embedder_identity

from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.services.model_credential import (
    ModelCredential,
    ModelCredentialUnavailable,
)

__all__ = [
    "Embedder",
    "HashingEmbedder",
    "OpenAIEmbedder",
    "embedder_identity",
    "make_embedder",
]


def make_embedder(settings: Settings, *, credential: ModelCredential | None = None) -> Embedder:
    if settings.embedder == "openai":
        if credential is None:
            raise ModelCredentialUnavailable(
                "embedding ingested content needs the organisation's model credential; none was "
                "resolved for this run",
                error_code="model_credential_not_configured",
            )
        return OpenAIEmbedder(
            api_key=credential.api_key,
            dim=settings.embedding_dim,
            base_url=settings.openai_base_url,
        )
    return HashingEmbedder(dim=settings.embedding_dim)
