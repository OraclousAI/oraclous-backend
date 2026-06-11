"""Unit tests for the LLM entity/relation extractor seam (KGS_EXTRACTOR=openai).

No real LLM, no network: a fake `LLMInterface` returns a fixed entity/relation JSON, so the wiring,
the chunk linkage, the honest counts, and the org-stamping are all exercised deterministically. The
`null` path (no extractor) is asserted to stay lexical-only.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from neo4j_graphrag.experimental.components.types import (
    Neo4jGraph,
    Neo4jNode,
    Neo4jRelationship,
)
from neo4j_graphrag.llm import LLMInterface
from neo4j_graphrag.llm.types import LLMResponse
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context
from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.repositories.graph_write_repository import (
    WriteResult,
    build_document_graph,
    chunk_node_ids,
)
from oraclous_knowledge_graph_service.services.embedder import HashingEmbedder
from oraclous_knowledge_graph_service.services.entity_extractor import (
    EntityExtractor,
    make_extractor,
)
from oraclous_knowledge_graph_service.services.ingestion_service import IngestionService

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")


def _ctx():
    return use_organisation_context(
        OrganisationContext(
            organisation_id=_ORG, principal_id=_ORG, principal_type=PrincipalType.SERVICE_ACCOUNT
        )
    )


class _FakeLLM(LLMInterface):
    """An LLMInterface whose every call returns the same fixed extraction JSON."""

    def __init__(self, *, nodes: list[dict], relationships: list[dict]) -> None:
        super().__init__(model_name="fake")
        self._payload = json.dumps({"nodes": nodes, "relationships": relationships})
        self.calls = 0

    def invoke(self, *args, **kwargs) -> LLMResponse:  # pragma: no cover - async path is used
        return LLMResponse(content=self._payload)

    async def ainvoke(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self._payload)


def _fixed_llm() -> _FakeLLM:
    return _FakeLLM(
        nodes=[
            {"id": "0", "label": "Person", "properties": {"name": "Ada"}},
            {"id": "1", "label": "Company", "properties": {"name": "Analytical Engines"}},
        ],
        relationships=[
            {"start_node_id": "0", "end_node_id": "1", "type": "WORKS_AT", "properties": {}},
        ],
    )


# --- the extractor in isolation ----------------------------------------------
async def test_extract_returns_entities_relationships_and_chunk_links() -> None:
    llm = _fixed_llm()
    extractor = EntityExtractor(llm=llm)
    chunk_ids = ["chunk-a", "chunk-b"]
    graph = await extractor.extract(chunks=["t one", "t two"], chunk_ids=chunk_ids)

    assert llm.calls == 2  # one LLM call per chunk
    # two entities per chunk -> four entity nodes
    labels = sorted(n.label for n in graph.nodes)
    assert labels == ["Company", "Company", "Person", "Person"]

    from_chunk = [r for r in graph.relationships if r.type == "FROM_CHUNK"]
    work_at = [r for r in graph.relationships if r.type == "WORKS_AT"]
    assert len(work_at) == 2  # one entity↔entity rel per chunk
    assert len(from_chunk) == 4  # every entity linked to its source chunk
    # every FROM_CHUNK edge points at one of the real (deterministic) chunk ids
    assert {r.end_node_id for r in from_chunk} == set(chunk_ids)


async def test_extract_node_ids_namespaced_by_chunk() -> None:
    """Same entity id from two chunks yields two distinct nodes (resolver merges later)."""
    extractor = EntityExtractor(llm=_fixed_llm())
    graph = await extractor.extract(chunks=["x", "y"], chunk_ids=["c0", "c1"])
    ids = [n.id for n in graph.nodes]
    assert len(ids) == len(set(ids))  # no collisions across chunks
    assert all(nid.startswith(("c0:", "c1:")) for nid in ids)


async def test_extract_is_deterministic_for_same_chunk_ids() -> None:
    a = await EntityExtractor(llm=_fixed_llm()).extract(chunks=["x"], chunk_ids=["c0"])
    b = await EntityExtractor(llm=_fixed_llm()).extract(chunks=["x"], chunk_ids=["c0"])
    assert [n.id for n in a.nodes] == [n.id for n in b.nodes]  # idempotent re-ingest


async def test_extract_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="same length"):
        await EntityExtractor(llm=_fixed_llm()).extract(chunks=["x"], chunk_ids=["c0", "c1"])


# --- build_document_graph merges the entity sub-graph ------------------------
def test_build_document_graph_merges_entity_graph() -> None:
    chunks = ["a", "b"]
    cids = chunk_node_ids(graph_id="g1", document="d", count=len(chunks))
    entity_graph = Neo4jGraph(
        nodes=[Neo4jNode(id="e0", label="Person", properties={"name": "Ada"})],
        relationships=[
            Neo4jRelationship(start_node_id="e0", end_node_id=cids[0], type="FROM_CHUNK")
        ],
    )
    merged = build_document_graph(
        graph_id="g1",
        document="d",
        chunks=chunks,
        embeddings=[[0.0], [0.0]],
        entity_graph=entity_graph,
    )
    labels = [n.label for n in merged.nodes]
    assert labels.count("Document") == 1
    assert labels.count("Chunk") == 2
    assert labels.count("Person") == 1  # entity merged in
    assert any(r.type == "FROM_CHUNK" for r in merged.relationships)


# --- IngestionService end-to-end against a capturing write repo --------------
class _CapturingWriteRepo:
    """Captures the graph the writer would persist, applying the real org-stamp via the writer."""

    def __init__(self) -> None:
        self.last_graph = None
        self.last_entity_graph = None

    async def write_document(
        self,
        *,
        graph_id,
        document,
        chunks,
        embeddings,
        title=None,
        entity_graph=None,
        ontology_violations=0,
        ontology_coercions=0,
    ):
        self.last_entity_graph = entity_graph
        self.last_violations = ontology_violations
        self.last_coercions = ontology_coercions
        self.last_graph = build_document_graph(
            graph_id=graph_id,
            document=document,
            chunks=chunks,
            embeddings=embeddings,
            entity_graph=entity_graph,
        )
        entities = len(entity_graph.nodes) if entity_graph else 0
        entity_rels = (
            sum(1 for r in entity_graph.relationships if r.type != "FROM_CHUNK")
            if entity_graph
            else 0
        )
        return WriteResult(
            nodes=len(self.last_graph.nodes),
            relationships=len(self.last_graph.relationships),
            chunks=len(chunks),
            entities=entities,
            entity_relationships=entity_rels,
            ontology_violations=ontology_violations,
            ontology_coercions=ontology_coercions,
        )


async def test_ingest_with_extractor_writes_entities_linked_to_chunks() -> None:
    repo = _CapturingWriteRepo()
    svc = IngestionService(repo, HashingEmbedder(dim=8), EntityExtractor(llm=_fixed_llm()))
    result = await svc.ingest(
        graph_id="g1", document="d.txt", data=b"para one\n\npara two", source_type="text"
    )
    # two chunks, two entities per chunk = 4 extracted entities; one WORKS_AT per chunk = 2
    assert result.entities == 4
    assert result.entity_relationships == 2
    # entities linked to the actual lexical chunk nodes
    chunk_ids = {n.id for n in repo.last_graph.nodes if n.label == "Chunk"}
    from_chunk_targets = {
        r.end_node_id for r in repo.last_entity_graph.relationships if r.type == "FROM_CHUNK"
    }
    assert from_chunk_targets <= chunk_ids
    assert from_chunk_targets  # non-empty


async def test_ingest_null_mode_is_lexical_only() -> None:
    repo = _CapturingWriteRepo()
    svc = IngestionService(repo, HashingEmbedder(dim=8), extractor=None)  # null mode
    result = await svc.ingest(
        graph_id="g1", document="d.txt", data=b"para one\n\npara two", source_type="text"
    )
    assert result.entities == 0
    assert result.entity_relationships == 0
    assert repo.last_entity_graph is None
    labels = {n.label for n in repo.last_graph.nodes}
    assert labels == {"Document", "Chunk"}  # no entity labels


# --- org-stamping: entities go through the org-scoped writer (T1) ------------
async def test_extracted_entities_are_org_stamped_by_the_writer() -> None:
    """The OrganisationScopedKGWriter stamps organisation_id on extracted entities too, and an
    LLM-supplied organisation_id is overwritten (cross-tenant leakage defence)."""
    captured = {}

    class _Base:
        driver = object()
        neo4j_database = "neo4j"

        async def run(self, graph):
            captured["graph"] = graph

    # an extractor whose entity tries to pin a foreign org id
    llm = _FakeLLM(
        nodes=[
            {
                "id": "0",
                "label": "Person",
                "properties": {"name": "Ada", "organisation_id": "FOREIGN-ORG"},
            }
        ],
        relationships=[],
    )
    entity_graph = await EntityExtractor(llm=llm).extract(chunks=["t"], chunk_ids=["chunk-a"])
    graph = build_document_graph(
        graph_id="g1", document="d", chunks=["t"], embeddings=[[0.0]], entity_graph=entity_graph
    )
    from oraclous_knowledge_graph_service.multi_tenant import OrganisationScopedKGWriter

    writer = OrganisationScopedKGWriter(base_writer=_Base(), graph_id="g1", ingestion_source="d")
    with _ctx():
        await writer.run(graph)

    person = next(n for n in captured["graph"].nodes if n.label == "Person")
    assert person.properties["organisation_id"] == str(_ORG)  # stamped to the bound org
    assert person.properties["organisation_id"] != "FOREIGN-ORG"  # foreign id overwritten


# --- make_extractor factory ---------------------------------------------------
def test_make_extractor_null_returns_none() -> None:
    assert make_extractor(Settings(extractor="null")) is None


def test_make_extractor_openai_requires_key() -> None:
    with pytest.raises(RuntimeError, match="KGS_OPENAI_API_KEY"):
        make_extractor(Settings(extractor="openai", openai_api_key=None))


def test_make_extractor_openai_builds_with_key() -> None:
    extractor = make_extractor(
        Settings(extractor="openai", openai_api_key="sk-test", extractor_model="openai/gpt-4o-mini")
    )
    assert isinstance(extractor, EntityExtractor)


def test_make_extractor_forwards_max_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """`KGS_EXTRACTOR_MAX_CONCURRENCY` -> settings.extractor_max_concurrency -> the extractor's
    library `max_concurrency` (env-tunable per-chunk LLM fan-out)."""
    monkeypatch.setenv("KGS_EXTRACTOR_MAX_CONCURRENCY", "17")
    settings = Settings(extractor="openai", openai_api_key="sk-test")
    assert settings.extractor_max_concurrency == 17  # env var maps via the KGS_ prefix

    extractor = make_extractor(settings)
    assert isinstance(extractor, EntityExtractor)
    assert extractor._extractor.max_concurrency == 17


def test_make_extractor_default_max_concurrency() -> None:
    """The default per-chunk concurrency is 10 (raised from 5 for throughput)."""
    extractor = make_extractor(Settings(extractor="openai", openai_api_key="sk-test"))
    assert isinstance(extractor, EntityExtractor)
    assert extractor._extractor.max_concurrency == 10


def test_make_extractor_forwards_schema_and_prompt_prefix() -> None:
    """make_extractor(schema=…, prompt_prefix=…) builds an extractor that carries both."""
    from neo4j_graphrag.experimental.components.schema import GraphSchema, NodeType

    schema = GraphSchema(node_types=(NodeType(label="Person"),))
    extractor = make_extractor(
        Settings(extractor="openai", openai_api_key="sk-test"),
        schema=schema,
        prompt_prefix="## hint",
    )
    assert isinstance(extractor, EntityExtractor)
    assert extractor._schema is schema
    assert extractor._prompt_prefix == "## hint"


# --- Slice B: the extractor passes BOTH schema + prompt_prefix into extract_for_chunk -----------
async def test_extract_passes_schema_and_prompt_prefix_to_extract_for_chunk() -> None:
    """A recording fake captures the (schema, examples) the EntityExtractor hands the library."""
    from neo4j_graphrag.experimental.components.schema import GraphSchema, NodeType

    schema = GraphSchema(node_types=(NodeType(label="Person"),))
    extractor = EntityExtractor(llm=_fixed_llm(), schema=schema, prompt_prefix="## steer")

    seen: dict = {}

    async def _recording_extract_for_chunk(passed_schema, examples, chunk):
        seen["schema"] = passed_schema
        seen["examples"] = examples
        return Neo4jGraph()

    extractor._extractor.extract_for_chunk = _recording_extract_for_chunk
    await extractor.extract(chunks=["t"], chunk_ids=["c0"])

    assert seen["schema"] is schema  # hard schema forwarded
    assert seen["examples"] == "## steer"  # prompt prefix forwarded (the {examples} slot)


# --- Slice B: free-text ontology enforcement (strict drops, coerce remaps) ----------------------
def _entity_graph(*labels: str) -> Neo4jGraph:
    nodes = [Neo4jNode(id=f"n{i}", label=lab) for i, lab in enumerate(labels)]
    rels = []
    if len(nodes) >= 2:
        rels.append(Neo4jRelationship(start_node_id="n0", end_node_id="n1", type="REL"))
    return Neo4jGraph(nodes=nodes, relationships=rels)


def test_enforce_ontology_open_is_passthrough() -> None:
    from oraclous_knowledge_graph_service.domain.ontology import Ontology
    from oraclous_knowledge_graph_service.services.ingestion_service import enforce_ontology

    g = _entity_graph("Person", "Company")
    out = enforce_ontology(g, Ontology(("Person",), "open"))
    assert out.graph is g  # untouched
    assert out.violations == 0 and out.coercions == 0


def test_enforce_ontology_strict_drops_off_type_entity_and_its_edges() -> None:
    from oraclous_knowledge_graph_service.domain.ontology import Ontology
    from oraclous_knowledge_graph_service.services.ingestion_service import enforce_ontology

    g = _entity_graph("Person", "Gadget")  # Gadget is off-ontology; the REL is incident to it
    out = enforce_ontology(g, Ontology(("Person",), "strict"))
    assert [n.label for n in out.graph.nodes] == ["Person"]  # Gadget dropped
    assert out.graph.relationships == []  # dangling edge to Gadget removed
    assert out.violations == 1
    assert out.coercions == 0


def test_enforce_ontology_coerce_remaps_near_match() -> None:
    from oraclous_knowledge_graph_service.domain.ontology import Ontology
    from oraclous_knowledge_graph_service.services.ingestion_service import enforce_ontology

    g = _entity_graph("Persons")  # near-match of allowed "Person"
    out = enforce_ontology(g, Ontology(("Person",), "coerce"))
    assert [n.label for n in out.graph.nodes] == ["Person"]  # remapped
    assert out.coercions == 1
    assert out.violations == 0


async def test_ingest_strict_ontology_drops_off_type_entity_before_write() -> None:
    """End-to-end through IngestionService: a strict graph never gains an off-ontology node."""
    from oraclous_knowledge_graph_service.domain.ontology import Ontology

    repo = _CapturingWriteRepo()
    # The LLM tries to extract Person + Company; the ontology only allows Person (strict).
    svc = IngestionService(
        repo,
        HashingEmbedder(dim=8),
        EntityExtractor(llm=_fixed_llm()),
        ontology=Ontology(("Person",), "strict"),
    )
    result = await svc.ingest(
        graph_id="g1", document="d.txt", data=b"para one\n\npara two", source_type="text"
    )
    # two chunks * (Person kept, Company dropped) -> 2 entities written, 2 violations
    written_labels = {n.label for n in repo.last_entity_graph.nodes}
    assert written_labels == {"Person"}
    assert result.entities == 2
    assert result.ontology_violations == 2  # one Company dropped per chunk
    assert result.entity_relationships == 0  # WORKS_AT edge dropped with Company


# --- ORAA-272: chunks extract CONCURRENTLY (capped), not one-await-at-a-time --------------------
class _ConcurrencyProbeLLM(LLMInterface):
    """Records the observed max number of in-flight `extract_for_chunk` calls.

    Each call increments a shared in-flight counter on entry, captures the running max, sleeps
    briefly so overlapping calls actually overlap, then decrements on exit. Chunk texts listed in
    `fail_on` raise instead of returning, to exercise the fail-soft (one bad chunk is skipped).
    """

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        super().__init__(model_name="probe")
        self._payload = json.dumps(
            {
                "nodes": [{"id": "0", "label": "Person", "properties": {"name": "Ada"}}],
                "relationships": [],
            }
        )
        self._fail_on = fail_on or set()
        self.in_flight = 0
        self.max_in_flight = 0

    def invoke(self, *args, **kwargs) -> LLMResponse:  # pragma: no cover - async path is used
        return LLMResponse(content=self._payload)

    async def ainvoke(self, input: str, *args, **kwargs) -> LLMResponse:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.02)
            if any(token in input for token in self._fail_on):
                raise RuntimeError("boom: this chunk's LLM call fails")
            return LLMResponse(content=self._payload)
        finally:
            self.in_flight -= 1


async def test_extract_runs_chunks_concurrently_capped_at_max_concurrency() -> None:
    """With max_concurrency=3 over 6 chunks, real overlap occurs but never exceeds the cap, and
    every chunk's entity lands in the combined graph."""
    llm = _ConcurrencyProbeLLM()
    extractor = EntityExtractor(llm=llm, max_concurrency=3)
    n = 6
    chunks = [f"chunk text {i}" for i in range(n)]
    chunk_ids = [f"c{i}" for i in range(n)]

    graph = await extractor.extract(chunks=chunks, chunk_ids=chunk_ids)

    # real, capped concurrency: more than one in flight, never more than the cap
    assert llm.max_in_flight > 1
    assert llm.max_in_flight <= 3
    # one Person per chunk -> all six entities present (identical result to the serial version)
    assert len(graph.nodes) == n
    assert all(n.label == "Person" for n in graph.nodes)
    # every chunk linked to its own deterministic chunk id
    from_chunk_targets = {r.end_node_id for r in graph.relationships if r.type == "FROM_CHUNK"}
    assert from_chunk_targets == set(chunk_ids)


async def test_extract_one_failing_chunk_is_logged_and_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A chunk whose LLM call raises is logged + skipped; the other chunks' entities still land
    (fail-soft, mirroring the library's on_error=IGNORE)."""
    import logging as _logging

    llm = _ConcurrencyProbeLLM(fail_on={"chunk text 2"})
    extractor = EntityExtractor(llm=llm, max_concurrency=3)
    n = 4
    chunks = [f"chunk text {i}" for i in range(n)]
    chunk_ids = [f"c{i}" for i in range(n)]

    with caplog.at_level(_logging.WARNING):
        graph = await extractor.extract(chunks=chunks, chunk_ids=chunk_ids)

    # the bad chunk (c2) contributed nothing; the other three each contributed one entity
    assert len(graph.nodes) == n - 1
    from_chunk_targets = {r.end_node_id for r in graph.relationships if r.type == "FROM_CHUNK"}
    assert from_chunk_targets == {"c0", "c1", "c3"}
    # the failure was logged (named the failing chunk id), not raised
    assert any(
        "c2" in rec.getMessage() and rec.levelno == _logging.WARNING for rec in caplog.records
    )


async def test_extract_stores_max_concurrency() -> None:
    """The constructor records max_concurrency for the across-chunk gather (not just library)."""
    extractor = EntityExtractor(llm=_fixed_llm(), max_concurrency=7)
    assert extractor._max_concurrency == 7
