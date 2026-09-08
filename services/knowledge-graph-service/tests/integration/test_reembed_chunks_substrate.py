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


def test_a_chunk_with_no_recorded_identity_is_stale_for_a_real_embedder(real_neo4j_driver) -> None:
    """A chunk written BEFORE the identity stamp existed carries no `embedder_id` at all — and
    those chunks are the ENTIRE corpus this migration exists for. Every pre-existing workspace is
    made of them.

    They are also the case the obvious predicate silently loses. Cypher is three-valued, so for an
    unstamped chunk and a non-legacy target, `c.embedder_id = 'openai:…'` is NULL, the legacy arm is
    false, `NULL OR false` is NULL, `NOT NULL` is NULL — and a `WHERE` that evaluates to NULL drops
    the row. A scan written that way finds every stale chunk EXCEPT the legacy ones: the sweep
    reports success on every cadence while never re-embedding a single pre-existing workspace, and
    those workspaces go on refusing meaning-based search with a message saying re-embedding is in
    progress when it is not, forever, with nothing anywhere saying so.

    The unknown identity must therefore read as "not current". That is the fail-safe direction
    (§3.5): re-embedding a chunk that did not need it costs one model call, skipping one that did
    costs that workspace its search.
    """
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    # Neo4j stores no property at all for a NULL value, so this chunk genuinely has no
    # `embedder_id` — the shape of every chunk ingested before the stamp, not a simulation of it.
    _seed_chunk(
        real_neo4j_driver, chunk_id="never-stamped", graph_id=graph_id, text="a", embedder_id=None
    )
    _seed_chunk(
        real_neo4j_driver,
        chunk_id="stamped-legacy",
        graph_id=graph_id,
        text="b",
        embedder_id=_HASHING_ID,
    )
    assert _stored_embedder_ids(real_neo4j_driver, graph_id=graph_id)["never-stamped"] is None

    run_reembed = _run_reembed()
    stats = run_reembed(
        _repo(real_neo4j_driver, graph_id=graph_id),
        _FakeEmbedder(),
        embedder_id=_OPENAI_ID,
        embedding_dim=512,
    )

    # BOTH, not just the explicitly-stamped one. A count of 1 here is the bug above.
    assert stats["reembedded"] == 2
    assert _stored_embedder_ids(real_neo4j_driver, graph_id=graph_id) == {
        "never-stamped": _OPENAI_ID,
        "stamped-legacy": _OPENAI_ID,
    }


def test_a_chunk_with_no_recorded_identity_is_current_for_the_legacy_target(
    real_neo4j_driver,
) -> None:
    """The other direction, and the reason "unknown is stale" cannot be spelled as a blanket
    `c.embedder_id IS NULL OR c.embedder_id <> $target`.

    An unstamped chunk predates the stamp, which means the keyless hashing embedder produced it.
    For a deployment still ON that embedder it is already in the target space, so it is CURRENT and
    must be left alone. Read the other way, every key-free deployment would re-embed its whole
    corpus on every sweep, forever, and never converge — the pass would have no fixed point.
    """
    _wipe(real_neo4j_driver)
    graph_id = str(uuid.uuid4())
    _seed_chunk(
        real_neo4j_driver, chunk_id="never-stamped", graph_id=graph_id, text="a", embedder_id=None
    )

    run_reembed = _run_reembed()
    stats = run_reembed(
        _repo(real_neo4j_driver, graph_id=graph_id),
        _FakeEmbedder(),
        embedder_id=_HASHING_ID,
        embedding_dim=512,
    )

    assert stats["reembedded"] == 0
    # Left exactly as found — not re-stamped with the identity it already implicitly carries.
    assert _stored_embedder_ids(real_neo4j_driver, graph_id=graph_id) == {"never-stamped": None}


def test_organisation_isolation_holds_for_the_stale_chunk_scan(real_neo4j_driver) -> None:
    """The re-embed repository is org-scoped like every other read/write in this service
    (CLAUDE.md §3.3) — another organisation's stale chunk must never be picked up.

    The graph id is deliberately SHARED with the foreign chunk: a colliding graph id is the only
    thing that could make an org-blind scan look correct, so it is exactly the condition worth
    seeding. `organisation_id` is what has to do the isolating here, not the graph id.
    """
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

    # `_stored_embedder_ids` reads back WITHOUT an organisation filter, on purpose: an org-scoped
    # read-back could not see the foreign row at all, so it could not tell "left alone" apart from
    # "deleted". Reading org-blind, the foreign chunk must still be there and must still carry the
    # identity it was seeded with. An empty read-back would mean the pass had removed another
    # organisation's data — the isolation VIOLATION this test exists to rule out, not the proof of
    # isolation. The equality also pins that no extra chunk appeared under this graph id.
    assert _stored_embedder_ids(real_neo4j_driver, graph_id=graph_id) == {"foreign": _HASHING_ID}

    # ...and the vector itself is untouched, not merely the stamp. A pass that rewrote a foreign
    # chunk's embedding while leaving its identity alone would still be a cross-tenant write, and
    # the identity check above would not catch it.
    records, _, _ = real_neo4j_driver.execute_query(
        "MATCH (c:Chunk {id: 'foreign'}) RETURN c.embedding AS v, c.organisation_id AS o"
    )
    assert [(r["v"], r["o"]) for r in records] == [([0.0], other_org)]
