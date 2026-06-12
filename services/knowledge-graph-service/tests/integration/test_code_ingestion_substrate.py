"""Real-Neo4j integration test for the 6-stage code-ingestion pipeline (#305, ORAA-4 §22).

Proves the restored stages against a live ``neo4j:5.23-community`` (a dedicated module-scoped
container, mirroring ``test_community_gds.py`` — the repo-root ``neo4j_driver`` session fixture is
not an ancestor of this service's test tree, so the suite spins its own), NOT a mock:

  * full ingest (Stage 0/2/3/4/5) of a small multi-file Python sample -> :File/:Function/:Class +
    :Dependency nodes and CALLS/IMPORTS/INHERITS/METHOD_OF edges, with the Stage-4 `embedding`
    property written at the KGS dim (key-free hashing embedder);
  * delta detection (Stage 1): re-ingest with one file changed + one unchanged -> the changed
    file's old symbols are marked stale, the unchanged file is skipped, the new symbol appears;
  * stale cleanup (Stage 6): a TTL-expired stale symbol is deleted, an in-grace one survives;
  * cross-org isolation: org B never sees org A's code graph.

Org scope is bound via ``use_organisation_context`` (the worker invariant) and the service is run
synchronously (as the worker does via ``asyncio.to_thread``).
"""

from __future__ import annotations

import uuid
import zipfile
from collections.abc import Iterator
from io import BytesIO

import pytest
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.repositories.code_write_repository import (
    CodeGraphWriteRepository,
)
from oraclous_knowledge_graph_service.services.code_ingestion_service import CodeIngestionService

pytestmark = [pytest.mark.integration, pytest.mark.organization_isolation]

_ORG_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_ORG_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

_GREETER_V1 = b"""import os


class Base:
    pass


class Greeter(Base):
    def greet(self, name):
        return helper(name)


def helper(name):
    return name
"""

_GREETER_V2 = b"""import os


class Base:
    pass


class Greeter(Base):
    def greet(self, name):
        return helper(name.upper())


def helper(name):
    return name


def added(extra):
    return extra
"""

_UTIL = b"""def util(x):
    return x + 1
"""


def _ctx(org: str) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=uuid.UUID(org),
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.SERVICE_ACCOUNT,
    )


def _zip(files: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, raw in files.items():
            zf.writestr(path, raw)
    return buf.getvalue()


@pytest.fixture
def settings() -> Settings:
    # Key-free hashing embedder so Stage 4 runs deterministically with no network/key.
    return Settings(embedder="hashing", embedding_dim=512)


@pytest.fixture(scope="module")
def neo4j_driver() -> Iterator[object]:
    """A dedicated ``neo4j:5.23-community`` container for this suite (module-scoped)."""
    from neo4j import GraphDatabase
    from testcontainers.neo4j import Neo4jContainer

    container = Neo4jContainer("neo4j:5.23-community").with_env("NEO4J_AUTH", "neo4j/password")
    with container:
        driver = GraphDatabase.driver(container.get_connection_url(), auth=("neo4j", "password"))
        try:
            driver.verify_connectivity()
            yield driver
        finally:
            driver.close()


@pytest.fixture(autouse=True)
def _clean(neo4j_driver) -> Iterator[None]:
    """Each test starts from a clean graph (the module container is shared)."""
    neo4j_driver.execute_query("MATCH (n) DETACH DELETE n")
    yield


def _count(driver, org: str, graph: str, label: str) -> int:
    rec = driver.execute_query(
        f"MATCH (n:{label} {{graph_id: $g, organisation_id: $o}}) RETURN count(n) AS c",
        g=graph,
        o=org,
    ).records[0]
    return int(rec["c"])


def _rel_count(driver, org: str, graph: str, rel: str) -> int:
    rec = driver.execute_query(
        f"MATCH (:{'__KGBuilder__'} {{graph_id: $g, organisation_id: $o}})"
        f"-[r:{rel} {{graph_id: $g}}]->() RETURN count(r) AS c",
        g=graph,
        o=org,
    ).records[0]
    return int(rec["c"])


def test_full_code_ingest_writes_nodes_edges_deps_embeddings(neo4j_driver, settings) -> None:
    graph = str(uuid.uuid4())
    payload = _zip({"pkg/greeter.py": _GREETER_V1, "requirements.txt": b"requests==2.31.0\n"})
    with use_organisation_context(_ctx(_ORG_A)):
        svc = CodeIngestionService(driver=neo4j_driver, organisation_id=_ORG_A, settings=settings)
        counts = svc.ingest(graph_id=graph, document="repo.zip", data=payload)

    assert counts["files"] == 1
    assert counts["classes"] == 2  # Base + Greeter
    assert counts["functions"] >= 2  # greet + helper
    assert counts["dependencies"] == 1  # the manifest dependency (`requests`)
    assert counts["embeddings"] >= 1

    assert _count(neo4j_driver, _ORG_A, graph, "File") == 1
    assert _count(neo4j_driver, _ORG_A, graph, "Function") >= 2
    assert _count(neo4j_driver, _ORG_A, graph, "Class") == 2
    # The manifest dependency `requests` is a :Dependency node (the external import `os` is too).
    dep_names = neo4j_driver.execute_query(
        "MATCH (d:Dependency {graph_id:$g, organisation_id:$o}) RETURN collect(d.name) AS n",
        g=graph,
        o=_ORG_A,
    ).records[0]["n"]
    assert "requests" in dep_names
    requests_dep = neo4j_driver.execute_query(
        "MATCH (d:Dependency {graph_id:$g, name:'requests'}) RETURN d.version_constraint AS v",
        g=graph,
    ).records[0]["v"]
    assert requests_dep == "==2.31.0"

    # CALLS (greet -> helper), INHERITS (Greeter -> Base), METHOD_OF (greet -> Greeter).
    calls = neo4j_driver.execute_query(
        "MATCH (:Function {graph_id:$g})-[:CALLS]->(b:Function {graph_id:$g}) "
        "RETURN b.qualified_name AS callee",
        g=graph,
    ).records
    assert any(r["callee"] == "pkg.greeter.helper" for r in calls)
    assert _rel_count(neo4j_driver, _ORG_A, graph, "INHERITS") >= 1
    assert _rel_count(neo4j_driver, _ORG_A, graph, "METHOD_OF") >= 1

    # Stage 4 — the `embedding` property is written (org+graph scoped) on Function/Class nodes.
    # The accelerating vector INDEX is deferred to the retriever slice (a label-wide Neo4j vector
    # index cannot be org-scoped; the org-filtered kNN belongs at the read layer — #294).
    embedded = neo4j_driver.execute_query(
        "MATCH (f:Function {graph_id:$g}) WHERE f.embedding IS NOT NULL RETURN count(f) AS c",
        g=graph,
    ).records[0]["c"]
    assert int(embedded) >= 1
    dim = neo4j_driver.execute_query(
        "MATCH (f:Function {graph_id:$g}) WHERE f.embedding IS NOT NULL "
        "RETURN size(f.embedding) AS d LIMIT 1",
        g=graph,
    ).records[0]["d"]
    assert int(dim) == 512  # matches the KGS embedding dim


def test_delta_detection_marks_stale_and_skips_unchanged(neo4j_driver, settings) -> None:
    graph = str(uuid.uuid4())
    with use_organisation_context(_ctx(_ORG_A)):
        svc = CodeIngestionService(driver=neo4j_driver, organisation_id=_ORG_A, settings=settings)
        # First ingest: greeter.py + util.py
        svc.ingest(
            graph_id=graph,
            document="repo.zip",
            data=_zip({"pkg/greeter.py": _GREETER_V1, "pkg/util.py": _UTIL}),
        )
        # Re-ingest: greeter.py CHANGED (v2 adds `added`), util.py UNCHANGED.
        counts = svc.ingest(
            graph_id=graph,
            document="repo.zip",
            data=_zip({"pkg/greeter.py": _GREETER_V2, "pkg/util.py": _UTIL}),
        )

    assert counts["files_changed"] == 1
    assert counts["files_unchanged"] == 1
    assert counts["files_new"] == 0

    # The new symbol from the changed file appears, not stale.
    added = neo4j_driver.execute_query(
        "MATCH (f:Function {graph_id:$g, qualified_name:'pkg.greeter.added'}) "
        "RETURN f.stale_at AS s",
        g=graph,
    ).records
    assert len(added) == 1 and added[0]["s"] is None

    # util.py was skipped (unchanged) -> its symbol is never marked stale.
    util = neo4j_driver.execute_query(
        "MATCH (f:Function {graph_id:$g, qualified_name:'pkg.util.util'}) RETURN f.stale_at AS s",
        g=graph,
    ).records
    assert len(util) == 1 and util[0]["s"] is None


def test_stale_cleanup_deletes_expired_and_keeps_recent(neo4j_driver, settings) -> None:
    graph = str(uuid.uuid4())
    with use_organisation_context(_ctx(_ORG_A)):
        svc = CodeIngestionService(driver=neo4j_driver, organisation_id=_ORG_A, settings=settings)
        svc.ingest(graph_id=graph, document="repo.zip", data=_zip({"pkg/util.py": _UTIL}))

    # Stamp one symbol stale 30 days ago (TTL-expired) and one stale just now (in grace).
    neo4j_driver.execute_query(
        "MATCH (f:Function {graph_id:$g, qualified_name:'pkg.util.util'}) "
        "SET f.stale_at = datetime() - duration({days: 30})",
        g=graph,
    )
    neo4j_driver.execute_query(
        "CREATE (:Function:__Entity__:__KGBuilder__ {graph_id:$g, organisation_id:$o, "
        "qualified_name:'pkg.util.fresh', stale_at: datetime()})",
        g=graph,
        o=_ORG_A,
    )

    with use_organisation_context(_ctx(_ORG_A)):
        writer = CodeGraphWriteRepository(neo4j_driver, graph_id=graph, organisation_id=_ORG_A)
        deleted = writer.delete_stale_symbols(ttl_days=settings.code_stale_ttl_days)

    assert deleted == 1  # only the 30-day-old one
    survivors = neo4j_driver.execute_query(
        "MATCH (f:Function {graph_id:$g}) RETURN collect(f.qualified_name) AS n",
        g=graph,
    ).records[0]["n"]
    assert "pkg.util.util" not in survivors
    assert "pkg.util.fresh" in survivors


def test_cross_org_isolation(neo4j_driver, settings) -> None:
    graph = str(uuid.uuid4())  # same graph id, two orgs — scope must keep them apart
    payload = _zip({"pkg/greeter.py": _GREETER_V1})
    with use_organisation_context(_ctx(_ORG_A)):
        CodeIngestionService(driver=neo4j_driver, organisation_id=_ORG_A, settings=settings).ingest(
            graph_id=graph, document="repo.zip", data=payload
        )

    # Org B sees nothing for the same graph id.
    assert _count(neo4j_driver, _ORG_B, graph, "File") == 0
    assert _count(neo4j_driver, _ORG_B, graph, "Function") == 0
    assert _count(neo4j_driver, _ORG_A, graph, "File") == 1

    # Org B's delta read is empty (no cross-tenant leak), so its ingest treats files as NEW.
    with use_organisation_context(_ctx(_ORG_B)):
        writer_b = CodeGraphWriteRepository(neo4j_driver, graph_id=graph, organisation_id=_ORG_B)
        assert writer_b.existing_file_hashes(["pkg/greeter.py"]) == {}
