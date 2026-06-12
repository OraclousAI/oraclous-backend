"""SQL connector helpers + relational recipe-path unit tests (#307).

Covers (no live DB — the live-Postgres introspection + ingest is the integration test):
  * DSN parse + dialect detection + egress integration (a blocked host raises);
  * the default relational recipe validates against the recipe schema;
  * the relational StructuralRepresentation → graph projection over a stateful in-memory writer:
    rows→entities keyed by PK, one node label per table, and FK→`REFERENCES_{TARGET}` edges resolved
    by matching the FK value to the target row's PK;
  * credential resolution (fake broker) returns the configured DSN, and a real broker resolves a
    connection_string by id over a mocked transport.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from oraclous_knowledge_graph_service.domain.connectors.sql_connector import (
    ColumnMeta,
    SchemaSnapshot,
    SqlConnectorError,
    SqlDialect,
    TableMeta,
    map_fk_relationship_type,
    parse_and_validate_dsn,
)
from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.services.credential_client import (
    CredentialResolutionError,
    FakeCredentialBroker,
    RealCredentialBroker,
)
from oraclous_knowledge_graph_service.services.recipes.engine import (
    _deterministic_id,
    get_recipe_engine,
)
from oraclous_knowledge_graph_service.services.structured.relational import (
    build_default_relational_recipe,
    decompose_relational,
)

pytestmark = pytest.mark.unit


# --- a stateful fake writer (models MERGE-by-id) — same contract as the engine FK test ---------
class _StatefulWriter:
    graph_id = "g-rel"

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: list[tuple[str, str, str]] = []

    def write_source(self, *, source_id, source_type, shape_signature, meta) -> None:
        pass

    def write_containers(self, *, label, rows, source_id, meta) -> None:
        pass

    def link_containers(self, *, pairs) -> None:
        pass

    def merge_node(
        self,
        *,
        label,
        entity_id,
        identity_key,
        properties,
        provenance,
        source_id,
        meta,
        confidence,
        container_id,
        aliases=None,
    ) -> None:
        node = self.nodes.setdefault(entity_id, {"label": label, "props": {}})
        node["label"] = label
        node["props"].update(properties)

    def set_property(self, *, prop_name, targets) -> int:
        for t in targets:
            if t["id"] in self.nodes:
                self.nodes[t["id"]]["props"][prop_name] = t["value"]
        return len(targets)

    def merge_edge(self, *, rel_type, edges, source_id, provenance, meta) -> int:
        for e in edges:
            self.edges.append((rel_type, e["from"], e["to"]))
        return len(edges)

    def merge_edge_to_stub(
        self, *, rel_type, target_label, edges, source_id, provenance, meta
    ) -> int:
        for e in edges:
            self.nodes.setdefault(e["to"], {"label": target_label, "props": {}})
            self.edges.append((rel_type, e["from"], e["to"]))
        return len(edges)

    def labels(self) -> list[str]:
        return [n["label"] for n in self.nodes.values()]

    def rels(self, rel_type: str) -> list[tuple[str, str]]:
        return [(f, t) for (rt, f, t) in self.edges if rt == rel_type]


# --- a small two-table schema with an FK -------------------------------------
def _employee_dept_snapshot() -> SchemaSnapshot:
    departments = TableMeta(
        name="departments",
        schema_name="public",
        columns=[
            ColumnMeta(name="id", data_type="integer", nullable=False, is_pk=True, is_fk=False),
            ColumnMeta(name="name", data_type="text", nullable=False, is_pk=False, is_fk=False),
        ],
    )
    employees = TableMeta(
        name="employees",
        schema_name="public",
        columns=[
            ColumnMeta(name="id", data_type="integer", nullable=False, is_pk=True, is_fk=False),
            ColumnMeta(name="name", data_type="text", nullable=False, is_pk=False, is_fk=False),
            ColumnMeta(
                name="dept_id",
                data_type="integer",
                nullable=True,
                is_pk=False,
                is_fk=True,
                fk_table="departments",
                fk_column="id",
            ),
        ],
    )
    return SchemaSnapshot(
        dialect=SqlDialect.POSTGRESQL,
        database="testdb",
        schema_name="public",
        tables=[departments, employees],
    )


# --- DSN parsing + egress integration ----------------------------------------
def test_parse_dsn_postgres_defaults_and_dialect() -> None:
    # A public literal-IP host avoids a real DNS lookup and is allowed in multi-tenant mode.
    p = parse_and_validate_dsn("postgresql://u:pw@93.184.216.34:6000/testdb", allow_private=False)
    assert p.dialect is SqlDialect.POSTGRESQL
    assert p.port == 6000
    assert p.user == "u"
    assert p.password == "pw"  # noqa: S105 — test literal, not a real secret
    assert p.database == "testdb"
    assert p.pinned_ip == "93.184.216.34"


def test_parse_dsn_mysql_default_port() -> None:
    p = parse_and_validate_dsn("mysql://root:pw@93.184.216.34/appdb", allow_private=False)
    assert p.dialect is SqlDialect.MYSQL
    assert p.port == 3306


def test_parse_dsn_strips_driver_suffix() -> None:
    p = parse_and_validate_dsn("postgresql+asyncpg://u:pw@192.168.0.9/db", allow_private=True)
    assert p.dialect is SqlDialect.POSTGRESQL


def test_parse_dsn_unsupported_scheme_raises() -> None:
    with pytest.raises(SqlConnectorError, match="unsupported"):
        parse_and_validate_dsn("mongodb://u:pw@93.184.216.34/db", allow_private=False)


def test_parse_dsn_missing_database_raises() -> None:
    with pytest.raises(SqlConnectorError, match="no database"):
        parse_and_validate_dsn("postgresql://u:pw@93.184.216.34", allow_private=False)


def test_parse_dsn_blocked_host_raises_via_egress() -> None:
    from oraclous_knowledge_graph_service.domain.tcp_egress import EgressBlockedError

    with pytest.raises(EgressBlockedError):
        parse_and_validate_dsn("postgresql://u:pw@169.254.169.254/db", allow_private=True)


def test_map_fk_relationship_type() -> None:
    assert map_fk_relationship_type("departments") == "REFERENCES_DEPARTMENTS"
    assert map_fk_relationship_type("user-table") == "REFERENCES_USER_TABLE"


# --- default relational recipe validates -------------------------------------
def test_default_relational_recipe_validates() -> None:
    snap = _employee_dept_snapshot()
    recipe = build_default_relational_recipe(snap)
    get_recipe_engine().validate(recipe)  # raises on failure
    assert recipe["applies_to"]["source_type"] == "relational"


# --- the relational projection over the engine -------------------------------
def test_relational_projection_rows_entities_and_fk_edges() -> None:
    snap = _employee_dept_snapshot()
    rows = {
        "departments": [{"id": 1, "name": "Engineering"}, {"id": 2, "name": "Sales"}],
        "employees": [
            {"id": 10, "name": "Ada", "dept_id": 1},
            {"id": 11, "name": "Babbage", "dept_id": 1},
            {"id": 12, "name": "Carol", "dept_id": 2},
        ],
    }
    rep = decompose_relational(snap, rows, ExtractionMode.FULL)
    recipe = build_default_relational_recipe(snap)
    writer = _StatefulWriter()
    get_recipe_engine().execute(recipe, rep, writer)

    labels = writer.labels()
    assert labels.count("Departments") == 2  # one node per department row
    assert labels.count("Employees") == 3  # one node per employee row, keyed by PK

    # FK → REFERENCES_DEPARTMENTS edges: one per employee, to the right department PK.
    refs = writer.rels("REFERENCES_DEPARTMENTS")
    assert len(refs) == 3
    dept1 = _deterministic_id("g-rel", "Departments", "1")
    dept2 = _deterministic_id("g-rel", "Departments", "2")
    emp10 = _deterministic_id("g-rel", "Employees", "10")
    emp12 = _deterministic_id("g-rel", "Employees", "12")
    assert (emp10, dept1) in refs  # Ada → Engineering
    assert (emp12, dept2) in refs  # Carol → Sales
    # A scalar property landed on the entity (non-PK column projected).
    assert writer.nodes[emp10]["props"]["name"] == "Ada"


def test_schema_only_emits_no_records() -> None:
    snap = _employee_dept_snapshot()
    rep = decompose_relational(snap, {}, ExtractionMode.SAMPLE)
    record_units = [u for u in rep.units if u.kind.value == "record"]
    assert record_units == []
    # The tables + columns are still represented (metadata projection).
    assert any(u.kind.value == "table" for u in rep.units)


# --- credential resolution ---------------------------------------------------
@pytest.mark.asyncio
async def test_fake_broker_returns_default_dsn() -> None:
    broker = FakeCredentialBroker(default_dsn="postgresql://u:pw@db/appdb")
    dsn = await broker.resolve_connection_string(
        organisation_id=str(uuid.uuid4()), credential_id="cred-1"
    )
    assert dsn == "postgresql://u:pw@db/appdb"
    await broker.aclose()
    assert broker.closed is True


@pytest.mark.asyncio
async def test_fake_broker_missing_dsn_raises() -> None:
    broker = FakeCredentialBroker()
    with pytest.raises(CredentialResolutionError, match="no DSN mapped"):
        await broker.resolve_connection_string(organisation_id="o", credential_id="x")


@pytest.mark.asyncio
async def test_real_broker_resolves_connection_string_by_id() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("X-Internal-Key")
        return httpx.Response(
            200, json={"credential": {"connection_string": "postgresql://x@h/db"}}
        )

    transport = httpx.MockTransport(handler)
    broker = RealCredentialBroker(
        base_url="http://broker:8004", internal_key="secret-key", transport=transport
    )
    dsn = await broker.resolve_connection_string(organisation_id="org-1", credential_id="cred-9")
    assert dsn == "postgresql://x@h/db"
    assert captured["url"].endswith("/internal/resolve-credential")
    assert captured["key"] == "secret-key"
    await broker.aclose()


@pytest.mark.asyncio
async def test_real_broker_404_raises_not_found() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(404))
    broker = RealCredentialBroker(base_url="http://b", internal_key="k", transport=transport)
    with pytest.raises(CredentialResolutionError, match="not found"):
        await broker.resolve_connection_string(organisation_id="o", credential_id="c")
    await broker.aclose()
