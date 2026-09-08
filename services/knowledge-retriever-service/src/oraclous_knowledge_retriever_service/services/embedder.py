"""Query embedder (services layer) — thin KRS wiring over the SHARED `oraclous_embedding` package.

#643: this service used to carry a lone, hand-duplicated `HashingEmbedder` — its own docstring
called it "BYTE-IDENTICAL to the knowledge-graph-service write-side embedder... If the two ever
diverge, semantic search silently degrades — keep them in lockstep." Nothing enforced that promise.
`HashingEmbedder`/`OpenAIEmbedder` are now IMPORTED (not re-implemented) from `packages/embedding`
(`oraclous_embedding`), the one shared implementation both services build their embedder from.

`resolve_embedder_for_org` (#643 proper, C2) is the query-side analogue of the write side's
`resolve_model_credential`/the proven `resolve_judge_for_org` (ADR-037): the org's OWN credential,
or a refusal (#949 Q1 — no platform-key fallback). Design call B3: the resolver takes ONLY
`(settings, organisation_id)` — no `graph_id`/`credential_id` — because the write side's per-graph
pinned credential (`knowledge_graphs.model_credential_id`) lives in the graph service's own
Postgres and this service cannot reach across for it; C3's embedder-identity comparison at read
time is what catches a graph pinned to a different model (refuses on mismatch), not this resolver.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
from oraclous_embedding import Embedder, HashingEmbedder, OpenAIEmbedder
from oraclous_embedding import embedder_identity as embedder_identity
from oraclous_embedding import make_embedder as make_embedder

from oraclous_knowledge_retriever_service.core.config import Settings
from oraclous_knowledge_retriever_service.services import credential_cache
from oraclous_knowledge_retriever_service.services.broker_client import BrokerClient, BrokerError

__all__ = [
    "Embedder",
    "HashingEmbedder",
    "OpenAIEmbedder",
    "embedder_identity",
    "make_embedder",
    "resolve_embedder_for_org",
]

#: The one refusal message for "this organisation has no model credential to embed with". Spelled
#: once so the cached-negative path and the live-broker path cannot say it two different ways; the
#: DI layer wraps it in the typed 422 rather than relaying this string.
_NO_CREDENTIAL = (
    "model_credential_not_configured: no model credential is configured for this organisation. "
    "Store one through the credentials API and designate it the organisation default."
)


@dataclass(frozen=True, repr=False)
class _ResolvedCredential:
    """A resolved BYOM model credential, held only for the embed call being built.

    `repr` is written by hand and REDACTS the key — mirrors the write side's `ModelCredential`
    (`services/model_credential.py` in knowledge-graph-service); this object crosses the cache and
    must never render its key into a log line or a traceback.
    """

    api_key: str
    credential_id: str

    def __repr__(self) -> str:
        return f"_ResolvedCredential(credential_id={self.credential_id!r}, api_key=<redacted>)"

    __str__ = __repr__


async def resolve_embedder_for_org(settings: Settings, *, organisation_id: uuid.UUID) -> Embedder:
    """The query embedder for this organisation: the org's own credential, or a refusal.

    `hashing` mode never touches the broker (the key-free CI/dev path, #949's explicit-offline
    carve-out) — it does not even construct a `BrokerClient`. `openai` mode resolves the org's
    default model credential (#949 Q1: no platform-key fallback, matching every other #724 site),
    cached process-locally (`services/credential_cache.py`, ported from the KGS module of the same
    name) so a second search for the same org does not pay a second broker round trip on the
    hottest path in this service.

    Raises :class:`BrokerError` when nothing is configured or nothing resolves — the DI layer
    turns that into the typed 422 (#949 Q4), never a fabricated/degraded search.
    """
    if settings.embedder != "openai":
        return make_embedder(settings)

    cached_default = credential_cache.get_default_id(organisation_id, "model")
    if cached_default is not None and cached_default[0] is None:
        # A cached "this org has designated nothing" is a real answer — refuse straight from the
        # cache rather than paying a round trip to be told the same thing on every search.
        raise BrokerError(_NO_CREDENTIAL)
    credential_id = cached_default[0] if cached_default is not None else None
    if credential_id is not None:
        cached_credential = credential_cache.get_credential(organisation_id, credential_id)
        if cached_credential is not None:
            return make_embedder(settings, credential=cached_credential)

    broker = BrokerClient(
        settings.credential_broker_url or "", internal_key=settings.internal_service_key or ""
    )
    try:
        if credential_id is None:
            credential_id = await broker.org_default_credential_id(
                organisation_id=organisation_id, purpose="model"
            )
            credential_cache.put_default_id(organisation_id, "model", credential_id)
        if not credential_id:
            raise BrokerError(_NO_CREDENTIAL)
        payload = await broker.resolve_credential(
            credential_id=credential_id, organisation_id=organisation_id
        )
    except httpx.HTTPError as exc:
        # `BrokerClient` raises `BrokerError` for a broker that ANSWERS badly, but a transport-level
        # failure (no broker configured at all → an empty base_url → httpx.UnsupportedProtocol)
        # escapes as an httpx error, which the DI layer's `except BrokerError` would miss and turn
        # into a 500. Fail closed as the same typed refusal, and name the deployment-level cause —
        # the caller cannot fix an unconfigured broker by storing a credential.
        raise BrokerError(
            "model_credential_not_configured: the credential broker could not be reached, so "
            "meaning-based search cannot resolve this organisation's model credential."
        ) from exc
    finally:
        await broker.aclose()

    api_key = payload.get("api_key") or payload.get("key")
    if not api_key:
        raise BrokerError(f"model credential {credential_id} holds no usable api_key")
    credential = _ResolvedCredential(api_key=str(api_key), credential_id=credential_id)
    credential_cache.put_credential(organisation_id, credential)
    return make_embedder(settings, credential=credential)
