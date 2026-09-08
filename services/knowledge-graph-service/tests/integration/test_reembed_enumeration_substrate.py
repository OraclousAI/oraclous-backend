"""Integration: the beat dispatcher's stale-graph enumeration, against real Neo4j (#949 Q2).

Coverage gap found at review: `enumerate_graphs_needing_reembed` was only ever exercised through a
monkeypatched fake, so its actual Cypher — the DISTINCT (org, graph) projection, the `NOT
_IS_CURRENT` predicate including its NULL reading, and the optional LIMIT — had never run against a
real store. It is the input to the whole automatic migration: a graph this query fails to list is a
workspace that silently never gets re-embedded, and its searches keep refusing forever with nobody
to notice, because the sweep reports success either way.

The predicate is shared with `list_chunks_needing_reembed`, but the enumeration is a different
statement over the WHOLE store rather than one bound scope, which is exactly why it needs its own
proof: it is the one re-embed query that is deliberately not org-scoped, since the beat worker has
no organisation of its own and must find every tenant's stale graphs to dispatch them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

pytestmark = pytest.mark.integration

_NEO4J_IMAGE = "neo4j:5.23-community"
_ORG_A = "00000000-0000-0000-0000-0000000005a0"
_ORG_B = "00000000-0000-0000-0000-0000000005b0"
_HASHING_ID = "hashing:512"
_OPENAI_ID = "openai:text-embedding-3-small:512"


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


def _enumerate():
    from oraclous_knowledge_graph_service.repositories.reembed_repository import (  # noqa: PLC0415
        enumerate_graphs_needing_reembed,
    )

    return enumerate_graphs_needing_reembed


def _wipe(driver) -> None:
    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")


def _seed_chunk(
    driver,
    *,
    graph_id: str,
    organisation_id: str = _ORG_A,
    embedder_id: str | None,
    text: str | None = "some text",
    chunk_id: str | None = None,
) -> None:
    driver.execute_query(
        "CREATE (:Chunk {id: $id, graph_id: $g, organisation_id: $o, text: $t, "
        "embedding: [0.0], embedder_id: $eid})",
        id=chunk_id or str(uuid.uuid4()),
        g=graph_id,
        o=organisation_id,
        t=text,
        eid=embedder_id,
    )


def test_a_graph_still_in_the_old_space_is_listed(real_neo4j_driver) -> None:
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _seed_chunk(real_neo4j_driver, graph_id=graph_id, embedder_id=_HASHING_ID)

    pairs = _enumerate()(real_neo4j_driver, embedder_id=_OPENAI_ID)

    assert pairs == [(_ORG_A, graph_id)]


def test_a_graph_already_in_the_target_space_is_not_listed(real_neo4j_driver) -> None:
    """Idempotence of the sweep itself: a finished workspace must stop being dispatched, or the
    beat re-queues a no-op job for it on every cadence forever."""
    _wipe(real_neo4j_driver)
    _seed_chunk(real_neo4j_driver, graph_id=str(uuid.uuid4()), embedder_id=_OPENAI_ID)

    assert _enumerate()(real_neo4j_driver, embedder_id=_OPENAI_ID) == []


def test_a_legacy_chunk_with_no_identity_counts_as_the_legacy_space(real_neo4j_driver) -> None:
    """A chunk written before the identity stamp carries no `embedder_id` at all. It reads as
    `hashing:512`, so it is STALE for an openai target and CURRENT for the hashing one — the same
    NULL reading the read-side filter applies, from the same shared constant."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _seed_chunk(real_neo4j_driver, graph_id=graph_id, embedder_id=None)

    assert _enumerate()(real_neo4j_driver, embedder_id=_OPENAI_ID) == [(_ORG_A, graph_id)]
    assert _enumerate()(real_neo4j_driver, embedder_id=_HASHING_ID) == []


def test_a_chunk_with_no_text_is_never_dispatched(real_neo4j_driver) -> None:
    """There is nothing to re-embed a chunk FROM without its text, and the per-graph pass skips it
    for the same reason — so listing its graph would dispatch a job that could never finish it."""
    _wipe(real_neo4j_driver)
    _seed_chunk(real_neo4j_driver, graph_id=str(uuid.uuid4()), embedder_id=_HASHING_ID, text=None)

    assert _enumerate()(real_neo4j_driver, embedder_id=_OPENAI_ID) == []


def test_one_graph_is_listed_once_however_many_stale_chunks_it_holds(real_neo4j_driver) -> None:
    """DISTINCT is load-bearing: without it a 900-chunk workspace would fan out 900 duplicate jobs,
    each contending for the same per-(org,graph) lock."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    for _ in range(5):
        _seed_chunk(real_neo4j_driver, graph_id=graph_id, embedder_id=_HASHING_ID)

    assert _enumerate()(real_neo4j_driver, embedder_id=_OPENAI_ID) == [(_ORG_A, graph_id)]


def test_every_organisations_stale_graphs_are_found(real_neo4j_driver) -> None:
    """The beat worker has no organisation of its own; this is the one re-embed query that must
    span tenants, or a whole organisation's workspaces never migrate. The per-graph job it
    dispatches is what re-applies the org scope."""
    _wipe(real_neo4j_driver)
    graph_a, graph_b = str(uuid.uuid4()), str(uuid.uuid4())
    _seed_chunk(real_neo4j_driver, graph_id=graph_a, organisation_id=_ORG_A, embedder_id=None)
    _seed_chunk(real_neo4j_driver, graph_id=graph_b, organisation_id=_ORG_B, embedder_id=None)

    pairs = _enumerate()(real_neo4j_driver, embedder_id=_OPENAI_ID)

    assert set(pairs) == {(_ORG_A, graph_a), (_ORG_B, graph_b)}
    # Each pair carries its OWN organisation — a graph dispatched under another organisation's id
    # would bind the per-graph pass to the wrong scope and re-embed nothing.
    assert {g: o for o, g in pairs} == {graph_a: _ORG_A, graph_b: _ORG_B}


def test_the_result_is_ordered_so_a_capped_sweep_is_reproducible(real_neo4j_driver) -> None:
    _wipe(real_neo4j_driver)
    graphs = sorted(str(uuid.uuid4()) for _ in range(4))
    for graph_id in graphs:
        _seed_chunk(real_neo4j_driver, graph_id=graph_id, embedder_id=_HASHING_ID)

    pairs = _enumerate()(real_neo4j_driver, embedder_id=_OPENAI_ID)

    assert [g for _, g in pairs] == graphs  # ORDER BY org, graph — not storage-order luck


def test_the_limit_bounds_one_sweeps_fan_out(real_neo4j_driver) -> None:
    """A sweep must not enqueue an unbounded fan-out. What the cap leaves behind is picked up next
    time, because the predicate is a fact about the store rather than a queue position."""
    _wipe(real_neo4j_driver)
    graphs = sorted(str(uuid.uuid4()) for _ in range(4))
    for graph_id in graphs:
        _seed_chunk(real_neo4j_driver, graph_id=graph_id, embedder_id=_HASHING_ID)

    capped = _enumerate()(real_neo4j_driver, embedder_id=_OPENAI_ID, limit=2)

    assert [g for _, g in capped] == graphs[:2]


def test_an_empty_store_dispatches_nothing(real_neo4j_driver) -> None:
    _wipe(real_neo4j_driver)

    assert _enumerate()(real_neo4j_driver, embedder_id=_OPENAI_ID) == []


# ── the per-graph scan shares the predicate, so it shares the trap ───────────────────────────


def _scan(driver, *, graph_id: str, embedder_id: str) -> list[str]:
    from oraclous_knowledge_graph_service.repositories.reembed_repository import (  # noqa: PLC0415
        ReembedRepository,
    )

    repo = ReembedRepository(driver, organisation_id=_ORG_A, graph_id=graph_id)
    return [r["id"] for r in repo.list_chunks_needing_reembed(embedder_id=embedder_id, limit=100)]


def test_the_pass_itself_picks_up_a_legacy_chunk_with_no_identity(real_neo4j_driver) -> None:
    """Being dispatched is only half of it: the per-graph scan uses the same predicate, so a job
    dispatched for a legacy workspace would otherwise arrive, find nothing to do, and report a
    completed pass that migrated none of it."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _seed_chunk(real_neo4j_driver, graph_id=graph_id, embedder_id=None, chunk_id="legacy")
    _seed_chunk(real_neo4j_driver, graph_id=graph_id, embedder_id=_OPENAI_ID, chunk_id="current")

    assert _scan(real_neo4j_driver, graph_id=graph_id, embedder_id=_OPENAI_ID) == ["legacy"]


def test_the_pass_leaves_a_legacy_chunk_alone_when_the_target_IS_the_legacy_space(
    real_neo4j_driver,
) -> None:
    """The NULL reading has to stay a reading, not a blanket "always stale" — a deployment still on
    the hashing embedder must not re-embed its whole corpus on every sweep, forever."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _seed_chunk(real_neo4j_driver, graph_id=graph_id, embedder_id=None, chunk_id="legacy")

    assert _scan(real_neo4j_driver, graph_id=graph_id, embedder_id=_HASHING_ID) == []
