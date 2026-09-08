"""#949 ruling Q3 (C3, read half) — a query vector whose embedder identity does not match the
stored chunks' identity is REFUSED, not scored (fail-closed, §3.5). This is the actual fix for
#643: today nothing stops a hashing-embedded query from being cosine-compared against
openai-embedded chunks (or vice-versa), which is meaningless but returns a plausible-looking score.

Real Neo4j (no fakes) so the enforcement is proven at the actual Cypher boundary, not reimplemented
in Python. `RetrievalService`/`RetrievalRepository.semantic` do not yet accept an `embedder_id`
parameter — every not-yet-built seam use below is FUNCTION-LOCAL
(`.claude/rules/tests-seam-imports.md`); this file collects cleanly and hard-fails RED
(`TypeError`/`AttributeError`/`ImportError`) until the `[impl]` lands.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_knowledge_retriever_service.repositories.retrieval_repository import (
    RetrievalRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_NEO4J_IMAGE = "neo4j:5.23-community"
_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")

_HASHING_ID = "hashing:512"
_OPENAI_ID = "openai:text-embedding-3-small:512"


def _ctx():
    return use_organisation_context(
        OrganisationContext(
            organisation_id=_ORG, principal_id=_ORG, principal_type=PrincipalType.USER
        )
    )


@pytest.fixture(scope="module")
def real_neo4j_driver() -> Iterator[object]:
    from neo4j import GraphDatabase
    from testcontainers.neo4j import Neo4jContainer

    with Neo4jContainer(_NEO4J_IMAGE).with_env("NEO4J_AUTH", "neo4j/password") as container:
        driver = GraphDatabase.driver(container.get_connection_url(), auth=("neo4j", "password"))
        try:
            driver.verify_connectivity()
            yield driver
        finally:
            driver.close()


def _embed(text: str, dim: int = 512) -> list[float]:
    from oraclous_embedding import HashingEmbedder  # noqa: PLC0415

    (vector,) = HashingEmbedder(dim=dim).embed([text])
    return vector


def _create_chunk(driver, *, graph_id: str, text: str, embedder_id: str | None) -> None:
    driver.execute_query(
        "CREATE (:Chunk {graph_id: $g, organisation_id: $o, text: $t, embedding: $e, "
        "embedder_id: $eid})",
        g=graph_id,
        o=str(_ORG),
        t=text,
        e=_embed(text),
        eid=embedder_id,
    )


def _wipe(driver) -> None:
    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")


def _service(driver, *, embedder_id: str):
    """Build the real service through the not-yet-built `embedder_id` constructor param, using a
    fixed-space hashing embedder as the QUERY embedder in every case (the vectors are irrelevant
    to these tests — only the identity FILTER is under test)."""
    from oraclous_embedding import HashingEmbedder  # noqa: PLC0415
    from oraclous_knowledge_retriever_service.services.retrieval_service import (  # noqa: PLC0415
        RetrievalService,
    )

    return RetrievalService(driver, HashingEmbedder(dim=512), embedder_id=embedder_id)


# --- the identity filter itself, at the repository boundary --------------------------------


def test_repository_semantic_only_returns_rows_matching_the_requested_identity(
    real_neo4j_driver,
) -> None:
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _create_chunk(
        real_neo4j_driver, graph_id=graph_id, text="hashing chunk", embedder_id=_HASHING_ID
    )
    _create_chunk(real_neo4j_driver, graph_id=graph_id, text="openai chunk", embedder_id=_OPENAI_ID)

    repo = RetrievalRepository(real_neo4j_driver, organisation_id=str(_ORG))
    rows = repo.semantic(
        graph_id=graph_id, qvec=_embed("hashing chunk"), top_k=10, embedder_id=_HASHING_ID
    )

    texts = {r["props"]["text"] for r in rows}
    assert texts == {"hashing chunk"}


def test_a_legacy_chunk_with_no_embedder_id_is_read_as_hashing_512(real_neo4j_driver) -> None:
    """Legacy chunks (written before #949) carry no `embedder_id` at all. They were written by the
    ORIGINAL hashing embedder, so a NULL must be treated as `hashing:512` — this is an explicit
    assumption that breaks if the hashing dimension ever changes, so it is pinned here rather than
    left implicit."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _create_chunk(real_neo4j_driver, graph_id=graph_id, text="legacy chunk", embedder_id=None)

    repo = RetrievalRepository(real_neo4j_driver, organisation_id=str(_ORG))
    rows = repo.semantic(
        graph_id=graph_id, qvec=_embed("legacy chunk"), top_k=10, embedder_id=_HASHING_ID
    )
    assert {r["props"]["text"] for r in rows} == {"legacy chunk"}


def test_a_legacy_null_chunk_does_not_match_an_openai_identity_query(real_neo4j_driver) -> None:
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _create_chunk(real_neo4j_driver, graph_id=graph_id, text="legacy chunk", embedder_id=None)

    repo = RetrievalRepository(real_neo4j_driver, organisation_id=str(_ORG))
    rows = repo.semantic(
        graph_id=graph_id, qvec=_embed("legacy chunk"), top_k=10, embedder_id=_OPENAI_ID
    )
    assert rows == []


# --- refused vs empty, at the service boundary ----------------------------------------------


async def test_a_graph_with_only_a_different_identity_is_refused_not_silently_empty(
    real_neo4j_driver,
) -> None:
    """The core #643 failure mode: a graph mid-re-embed (or pinned to a different model) holds
    chunks in a space the query embedder cannot compare against. That must surface as a REFUSAL
    the caller can act on — never a silent, plausible-looking empty result."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _create_chunk(real_neo4j_driver, graph_id=graph_id, text="openai only", embedder_id=_OPENAI_ID)

    from oraclous_knowledge_retriever_service.services.retrieval_service import (  # noqa: PLC0415
        EmbedderIdentityMismatch,
    )

    svc = _service(real_neo4j_driver, embedder_id=_HASHING_ID)
    with _ctx(), pytest.raises(EmbedderIdentityMismatch):
        await svc.semantic(graph_id=graph_id, query="openai only", top_k=10)


async def test_a_graph_with_no_chunks_at_all_is_genuinely_empty_not_refused(
    real_neo4j_driver,
) -> None:
    """A nonexistent/empty graph is the ordinary "no results" case (e.g. a wrong graph_id) — it
    must NOT be conflated with the refusal above, or every miss becomes a scary error."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())

    svc = _service(real_neo4j_driver, embedder_id=_HASHING_ID)
    with _ctx():
        results = await svc.semantic(graph_id=graph_id, query="anything", top_k=10)
    assert results == []


async def test_a_mixed_graph_returns_only_the_matching_identitys_chunks(real_neo4j_driver) -> None:
    """C6's re-embed task leaves a graph legitimately MIXED mid-run — the filter, not a pre-check,
    is what keeps that honest: same-identity chunks still score, foreign-identity chunks are
    excluded from comparison entirely (never compared across spaces)."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _create_chunk(
        real_neo4j_driver, graph_id=graph_id, text="hashing chunk", embedder_id=_HASHING_ID
    )
    _create_chunk(real_neo4j_driver, graph_id=graph_id, text="openai chunk", embedder_id=_OPENAI_ID)

    svc = _service(real_neo4j_driver, embedder_id=_HASHING_ID)
    with _ctx():
        results = await svc.semantic(graph_id=graph_id, query="hashing chunk", top_k=10)

    assert len(results) == 1
    assert results[0]["properties"]["text"] == "hashing chunk"
