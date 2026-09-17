"""Unit: the embed CALL fails closed, and a rejected credential heals itself (#643).

Review findings on the #643 implementation. The DI layer refused cleanly when an organisation had
no credential to embed with, but `RetrievalService.semantic()` then called `embed()` unguarded — so
every failure of the call ITSELF (provider unreachable, rate-limited, or the key revoked inside the
60s cache TTL) left the service as a bare 500 carrying raw provider text, on the one path whose
whole premise is "fail closed, never a raw provider error". And `credential_cache.invalidate()` was
ported into this service but never called, while the module's own docstring promised rotation
"self-healing within one call".

Both halves are pinned here: the refusal is typed and carries none of the provider's own words
(rule 8), and a rejection actually drops the cached credential so the next search re-resolves.

#1109 ruling 3 splits a THIRD case out of the same call: a 429/quota EXHAUSTED credential is
neither "bad key" (401/403) nor "provider unreachable" — it raises a new
``QueryEmbeddingCredentialExhausted`` (a sibling of ``QueryEmbeddingCredentialRejected``, both under
``QueryEmbeddingUnavailable``) and still drops the cached credential, same as a rejection.
``QueryEmbeddingCredentialExhausted`` does not exist yet; every reference to it is FUNCTION-LOCAL
(`.claude/rules/tests-seam-imports.md`) so this module collects cleanly and hard-fails RED
(ImportError) until the `[impl]` lands.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_knowledge_retriever_service.services import credential_cache
from oraclous_knowledge_retriever_service.services.retrieval_service import (
    QueryEmbeddingCredentialRejected,
    QueryEmbeddingUnavailable,
    RetrievalService,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("a35472b5-9490-4e22-bf20-3399a5462f5a")

#: A provider message shaped like the real thing — it names an internal host, which is exactly what
#: must not reach the caller (rule 8).
_LEAKY_401 = "AuthenticationError: 401 Unauthorized from http://broker.internal:8004/v1/embeddings"


def _ctx():
    return use_organisation_context(
        OrganisationContext(
            organisation_id=_ORG, principal_id=_ORG, principal_type=PrincipalType.USER
        )
    )


class _BrokenEmbedder:
    dim = 8

    def __init__(self, error: Exception) -> None:
        self._error = error

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise self._error


class _StubCredential:
    """Enough of a resolved credential for the cache to hold one."""

    credential_id = "cred-1"
    api_key = "sk-live-not-a-real-key"


def _service(error: Exception) -> RetrievalService:
    return RetrievalService(None, _BrokenEmbedder(error), embedder_id="hashing:512")


@pytest.fixture(autouse=True)
def _clean_cache():
    credential_cache.clear()
    yield
    credential_cache.clear()


# ── the call fails closed ────────────────────────────────────────────────────────────────────


async def test_an_unreachable_provider_refuses_instead_of_raising_to_a_500() -> None:
    svc = _service(ConnectionError("connection refused"))
    with _ctx(), pytest.raises(QueryEmbeddingUnavailable):
        await svc.semantic(graph_id="g1", query="anything", top_k=10)


async def test_a_rejected_credential_is_a_distinct_refusal_from_an_unreachable_provider() -> None:
    """The two need different answers — one is the organisation's to fix, the other is ours — so
    they must not collapse into one exception the route cannot tell apart."""
    svc = _service(RuntimeError(_LEAKY_401))
    with _ctx(), pytest.raises(QueryEmbeddingCredentialRejected):
        await svc.semantic(graph_id="g1", query="anything", top_k=10)


async def test_the_providers_own_text_never_reaches_the_refusal() -> None:
    """Rule 8: provider errors can name internal hosts. The curated line is what a caller reads."""
    svc = _service(RuntimeError(_LEAKY_401))
    with _ctx(), pytest.raises(QueryEmbeddingUnavailable) as caught:
        await svc.semantic(graph_id="g1", query="anything", top_k=10)

    assert "broker.internal" not in str(caught.value)
    assert "8004" not in str(caught.value)


async def test_hybrid_refuses_on_the_same_failure_rather_than_falling_back_to_word_overlap() -> (
    None
):
    """Combined mode is built on the semantic list. Silently serving the lexical half under a
    `hybrid` label when the embedder failed is the degrade #643 exists to close."""
    svc = _service(ConnectionError("connection refused"))
    with _ctx(), pytest.raises(QueryEmbeddingUnavailable):
        await svc.hybrid(graph_id="g1", query="anything", top_k=10)


# ── rotation heals within one call ───────────────────────────────────────────────────────────


async def test_a_rejection_drops_this_organisations_cached_credential() -> None:
    """The cache's revocation window is its TTL, so a key revoked mid-TTL would otherwise keep
    being reused on the hottest path in the service until it expired."""
    credential_cache.put_credential(_ORG, _StubCredential())
    credential_cache.put_default_id(_ORG, "model", "cred-1")
    assert credential_cache.get_credential(_ORG, "cred-1") is not None

    svc = _service(RuntimeError(_LEAKY_401))
    with _ctx(), pytest.raises(QueryEmbeddingCredentialRejected):
        await svc.semantic(graph_id="g1", query="anything", top_k=10)

    assert credential_cache.get_credential(_ORG, "cred-1") is None
    assert credential_cache.get_default_id(_ORG, "model") is None


async def test_an_unreachable_provider_keeps_the_cached_credential() -> None:
    """A network fault says nothing about whether the key is good. Flushing on it would turn every
    blip into a broker round-trip storm for no benefit."""
    credential_cache.put_credential(_ORG, _StubCredential())

    svc = _service(ConnectionError("connection refused"))
    with _ctx(), pytest.raises(QueryEmbeddingUnavailable):
        await svc.semantic(graph_id="g1", query="anything", top_k=10)

    assert credential_cache.get_credential(_ORG, "cred-1") is not None


async def test_a_rejection_of_one_organisation_leaves_another_organisations_credential_alone() -> (
    None
):
    """Tenancy: the flush is scoped to the org whose provider call was rejected (§3.3)."""
    other = uuid.UUID("b35472b5-9490-4e22-bf20-3399a5462f5a")
    credential_cache.put_credential(_ORG, _StubCredential())
    credential_cache.put_credential(other, _StubCredential())

    svc = _service(RuntimeError(_LEAKY_401))
    with _ctx(), pytest.raises(QueryEmbeddingCredentialRejected):
        await svc.semantic(graph_id="g1", query="anything", top_k=10)

    assert credential_cache.get_credential(_ORG, "cred-1") is None
    assert credential_cache.get_credential(other, "cred-1") is not None


# ── an EXHAUSTED credential is a distinct refusal from a REJECTED one (#1109 ruling 3) ──────────

#: Shaped like the real thing — a 429/quota provider message, which must classify differently from
#: a 401/403 one even though both currently trip the SAME shared `is_credential_failure` marker
#: list this service's embed call is gated on.
_LEAKY_429 = "RateLimitError: 429 insufficient_quota from http://broker.internal:8004/v1/embeddings"


def _exhausted_exception_class():
    from oraclous_knowledge_retriever_service.services.retrieval_service import (  # noqa: PLC0415
        QueryEmbeddingCredentialExhausted,
    )

    return QueryEmbeddingCredentialExhausted


async def test_a_quota_exhausted_credential_raises_a_distinct_refusal_from_a_rejected_one() -> None:
    """The fix for the two differs — replace the key vs. wait/upgrade — so the caller must be able
    to tell them apart rather than both collapsing into one "credential is bad" refusal."""
    exhausted = _exhausted_exception_class()
    svc = _service(RuntimeError(_LEAKY_429))
    with _ctx(), pytest.raises(exhausted) as caught:
        await svc.semantic(graph_id="g1", query="anything", top_k=10)

    assert not isinstance(caught.value, QueryEmbeddingCredentialRejected)


async def test_the_exhausted_refusal_is_still_a_queryembeddingunavailable() -> None:
    """A caller catching the parent (as the route already does) must still catch this new sibling
    — it is a NARROWER `QueryEmbeddingUnavailable`, never a disjoint exception hierarchy."""
    exhausted = _exhausted_exception_class()
    assert issubclass(exhausted, QueryEmbeddingUnavailable)

    svc = _service(RuntimeError(_LEAKY_429))
    with _ctx(), pytest.raises(QueryEmbeddingUnavailable):
        await svc.semantic(graph_id="g1", query="anything", top_k=10)


async def test_an_exhausted_credential_also_drops_the_cached_credential() -> None:
    """Same self-healing promise as a rejection: an exhausted key is still resolved fresh next
    call, in case the org has since rotated it."""
    exhausted = _exhausted_exception_class()
    credential_cache.put_credential(_ORG, _StubCredential())
    credential_cache.put_default_id(_ORG, "model", "cred-1")

    svc = _service(RuntimeError(_LEAKY_429))
    with _ctx(), pytest.raises(exhausted):
        await svc.semantic(graph_id="g1", query="anything", top_k=10)

    assert credential_cache.get_credential(_ORG, "cred-1") is None
    assert credential_cache.get_default_id(_ORG, "model") is None


async def test_hybrid_also_raises_the_exhausted_refusal_rather_than_a_generic_one() -> None:
    exhausted = _exhausted_exception_class()
    svc = _service(RuntimeError(_LEAKY_429))
    with _ctx(), pytest.raises(exhausted):
        await svc.hybrid(graph_id="g1", query="anything", top_k=10)
