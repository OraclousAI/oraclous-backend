"""#949 ruling Q2 (C6) — the re-embed pass proven against real Neo4j: restartable, idempotent,
leaves chunk ids untouched, and correctly reports per-workspace completion.

Drives `run_reembed` (the lock-free core `tasks/reembed_tasks.py` exposes precisely so this test
can call it directly against real substrate, mirroring `memory_tasks.run_consolidation`'s own
integration-testability) against a real Neo4j container — no worker, no broker, no Celery.

Every not-yet-built seam use is FUNCTION-LOCAL (`.claude/rules/tests-seam-imports.md`); this file
hard-fails RED with `ModuleNotFoundError` until the `[impl]` lands.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_NEO4J_IMAGE = "neo4j:5.23-community"
_ORG = "00000000-0000-0000-0000-00000000050a"
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


def _wipe(driver) -> None:
    with driver.session() as s:
        s.run("MATCH (n) DETACH DELETE n")


def _seed_chunk(
    driver, *, chunk_id: str, graph_id: str, text: str, embedder_id: str | None
) -> None:
    driver.execute_query(
        "CREATE (:Chunk {id: $id, graph_id: $g, organisation_id: $o, text: $t, "
        "embedding: [0.0], embedder_id: $eid})",
        id=chunk_id,
        g=graph_id,
        o=_ORG,
        t=text,
        eid=embedder_id,
    )


def _repo(driver, *, graph_id: str):
    from oraclous_knowledge_graph_service.repositories.reembed_repository import (  # noqa: PLC0415, E501
        ReembedRepository,
    )

    return ReembedRepository(driver, organisation_id=_ORG, graph_id=graph_id)


def _run_reembed():
    from oraclous_knowledge_graph_service.tasks.reembed_tasks import run_reembed  # noqa: PLC0415

    return run_reembed


class _FakeEmbedder:
    dim = 512

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * self.dim for _ in texts]


def _stored_embedder_ids(driver, *, graph_id: str) -> dict[str, str | None]:
    records, _, _ = driver.execute_query(
        "MATCH (c:Chunk) WHERE c.graph_id = $g RETURN c.id AS id, c.embedder_id AS eid", g=graph_id
    )
    return {r["id"]: r["eid"] for r in records}


def test_stale_chunks_are_rewritten_to_the_target_identity(real_neo4j_driver) -> None:
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _seed_chunk(
        real_neo4j_driver, chunk_id="c1", graph_id=graph_id, text="a", embedder_id=_HASHING_ID
    )
    _seed_chunk(
        real_neo4j_driver, chunk_id="c2", graph_id=graph_id, text="b", embedder_id=_HASHING_ID
    )

    run_reembed = _run_reembed()
    stats = run_reembed(
        _repo(real_neo4j_driver, graph_id=graph_id),
        _FakeEmbedder(),
        embedder_id=_OPENAI_ID,
        embedding_dim=512,
    )

    assert stats["reembedded"] == 2
    assert set(_stored_embedder_ids(real_neo4j_driver, graph_id=graph_id).values()) == {_OPENAI_ID}


def test_chunk_ids_never_change_across_a_reembed(real_neo4j_driver) -> None:
    """The plan is explicit: the text does not change, so the id does not change — a
    reference to a chunk elsewhere in the graph must never break."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _seed_chunk(
        real_neo4j_driver, chunk_id="c1", graph_id=graph_id, text="a", embedder_id=_HASHING_ID
    )

    before = set(_stored_embedder_ids(real_neo4j_driver, graph_id=graph_id).keys())
    run_reembed = _run_reembed()
    run_reembed(
        _repo(real_neo4j_driver, graph_id=graph_id),
        _FakeEmbedder(),
        embedder_id=_OPENAI_ID,
        embedding_dim=512,
    )
    after = set(_stored_embedder_ids(real_neo4j_driver, graph_id=graph_id).keys())

    assert before == after == {"c1"}


def test_a_second_run_over_an_already_reembedded_graph_touches_nothing(real_neo4j_driver) -> None:
    """Idempotent: a completed workspace is a no-op."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _seed_chunk(
        real_neo4j_driver, chunk_id="c1", graph_id=graph_id, text="a", embedder_id=_OPENAI_ID
    )

    run_reembed = _run_reembed()
    stats = run_reembed(
        _repo(real_neo4j_driver, graph_id=graph_id),
        _FakeEmbedder(),
        embedder_id=_OPENAI_ID,
        embedding_dim=512,
    )

    assert stats["reembedded"] == 0


def test_two_consecutive_runs_never_double_process_the_same_chunk(real_neo4j_driver) -> None:
    """Restartable: a worker rescheduled after a crash calls `run_reembed` again with no special
    resume state — the identity predicate alone must make that safe. Two consecutive calls over a
    small `batch_size` (so each internal fetch only ever sees a slice) finish the whole workspace
    between them and never rewrite the same chunk twice."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    for i in range(3):
        _seed_chunk(
            real_neo4j_driver,
            chunk_id=f"c{i}",
            graph_id=graph_id,
            text=f"chunk {i}",
            embedder_id=_HASHING_ID,
        )

    run_reembed = _run_reembed()
    repo = _repo(real_neo4j_driver, graph_id=graph_id)
    kwargs = {"embedder_id": _OPENAI_ID, "embedding_dim": 512, "batch_size": 2}
    first = run_reembed(repo, _FakeEmbedder(), **kwargs)
    second = run_reembed(repo, _FakeEmbedder(), **kwargs)

    assert first["reembedded"] + second["reembedded"] == 3
    assert set(_stored_embedder_ids(real_neo4j_driver, graph_id=graph_id).values()) == {_OPENAI_ID}


def test_a_mixed_graph_leaves_the_already_current_chunk_untouched(real_neo4j_driver) -> None:
    """Mid-run, a graph is legitimately mixed. `run_reembed` must only ever touch chunks that do
    NOT match the target identity — a chunk already in the target space is left alone."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _seed_chunk(
        real_neo4j_driver,
        chunk_id="already-current",
        graph_id=graph_id,
        text="a",
        embedder_id=_OPENAI_ID,
    )
    _seed_chunk(
        real_neo4j_driver, chunk_id="stale", graph_id=graph_id, text="b", embedder_id=_HASHING_ID
    )

    run_reembed = _run_reembed()
    stats = run_reembed(
        _repo(real_neo4j_driver, graph_id=graph_id),
        _FakeEmbedder(),
        embedder_id=_OPENAI_ID,
        embedding_dim=512,
    )

    assert stats["reembedded"] == 1


def test_organisation_isolation_holds_for_the_stale_chunk_scan(real_neo4j_driver) -> None:
    """The re-embed repository is org-scoped like every other read/write in this service
    (CLAUDE.md §3.3) — another organisation's stale chunk must never be picked up."""
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    other_org = "99999999-9999-9999-9999-999999999999"
    real_neo4j_driver.execute_query(
        "CREATE (:Chunk {id: 'foreign', graph_id: $g, organisation_id: $o, text: 'x', "
        "embedding: [0.0], embedder_id: $eid})",
        g=graph_id,
        o=other_org,
        eid=_HASHING_ID,
    )

    run_reembed = _run_reembed()
    stats = run_reembed(
        _repo(real_neo4j_driver, graph_id=graph_id),
        _FakeEmbedder(),
        embedder_id=_OPENAI_ID,
        embedding_dim=512,
    )

    assert stats["reembedded"] == 0
    assert _stored_embedder_ids(real_neo4j_driver, graph_id=graph_id) == {}
