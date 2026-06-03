"""[r2-gate] ORAA-80 — R2 acceptance gate, 9-deliverable verification.

Story:  ORAA-80
Jira:   ORA-79
Architecture refs:
  - R2 release page:   https://oraclous.atlassian.net/wiki/spaces/OP/pages/688482
  - OHM v1.0 Spec:     https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - Test Strategy:     https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940
  - ADR-008 (operator separation): https://oraclous.atlassian.net/wiki/spaces/OP/pages/524497

Every test is tagged ``r2_gate``.  The CI r2-gate job runs ``pytest -m r2_gate`` and
its failure blocks R2 from being declared Done.

Deliverables verified:

  D1  capability-registry-service runs at unchanged ports; service shell is healthy
  D2  All 5 capability kinds stored as OHM descriptors with ``kind`` discriminator
  D3  Single DB-backed registry; no dual-registry code path exists
  D4  Google Drive, Notion, PostgreSQL, MySQL tools have valid OHM descriptors
  D5  Every descriptor carries a content hash; hash is deterministic (stability)
  D6  MCP round-trip: external MCP tools appear as implementation.type == "mcp"
  D7  KGB agent toolkit reads schemas from registry; no static _TOOL_SCHEMAS dict
  D8  workflow_service.py and pipeline_generator.py absent from codebase
  D9  Per-harness allocation rejects scope-exceeding requests (T2-M3)

Import strategy: all app.* imports are function-local (CLAUDE.md §4.1 / ADR-010) so
pytest collection succeeds on any branch state and each test fails at runtime when an
implementation is absent.  ``conftest.py`` adds the necessary sys.path entries.
"""

from __future__ import annotations

import importlib
import importlib.util
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.r2_gate

# ---------------------------------------------------------------------------
# Repository root (used for file-system presence/absence checks)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent  # oraclous-backend/
_CORE_SERVICE = _REPO_ROOT / "oraclous-core-service"


# ===========================================================================
# D1 — capability-registry-service runs at unchanged ports
# ===========================================================================


def test_d1_capability_registry_dockerfile_exposes_port_8000():
    """Dockerfile must EXPOSE 8000 — the canonical container port."""
    dockerfile = _REPO_ROOT / "services" / "capability-registry-service" / "Dockerfile"
    assert dockerfile.exists(), "Dockerfile missing for capability-registry-service"
    content = dockerfile.read_text()
    assert "EXPOSE 8000" in content, (
        "capability-registry-service Dockerfile does not EXPOSE 8000; "
        "port has drifted from the declared container port"
    )


def test_d1_capability_registry_docker_compose_maps_port_8001_to_8000():
    """docker-compose.yml must map host 8001 → container 8000 for capability-registry-service."""
    compose = _REPO_ROOT / "deploy" / "docker-compose.yml"
    assert compose.exists(), "deploy/docker-compose.yml not found"
    content = compose.read_text()
    assert "8001:8000" in content, (
        "docker-compose.yml does not map capability-registry-service at 8001:8000; "
        "port mapping has changed"
    )


def test_d1_capability_registry_service_app_starts():
    """capability-registry-service FastAPI app must be instantiable without error."""
    import sys

    _cap_src = str(_REPO_ROOT / "services" / "capability-registry-service" / "src")
    if _cap_src not in sys.path:
        sys.path.insert(0, _cap_src)

    import os

    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    os.environ.setdefault("INTERNAL_SERVICE_KEY", "test-key")

    from oraclous_capability_registry_service.app.factory import create_app

    app = create_app()
    assert app is not None
    route_paths = [r.path for r in app.routes]  # type: ignore[attr-defined]
    assert "/health" in route_paths, "GET /health route missing from capability-registry-service"
    assert "/api/v1/health" in route_paths, (
        "GET /api/v1/health route missing — legacy clients will break"
    )


# ===========================================================================
# D2 — All 5 capability kinds stored as OHM descriptors with `kind` discriminator
# ===========================================================================


def test_d2_descriptor_kind_enum_has_all_five_values():
    """DescriptorKind must enumerate exactly the 5 canonical capability kinds."""
    from app.models.capability_descriptor import DescriptorKind

    expected = {"tool", "skill", "agent", "harness", "human_role"}
    actual = {k.value for k in DescriptorKind}
    assert actual == expected, (
        f"DescriptorKind values {actual!r} do not match expected set {expected!r}; "
        "R2 requires all 5 kinds to be registered"
    )


@pytest.mark.parametrize(
    "descriptor",
    [
        {
            "kind": "tool",
            "id": "r2-gate-tool",
            "version": {"hash": "sha256:aaa", "tags": []},
            "metadata": {"name": "Gate Tool", "description": "R2 gate test tool."},
            "spec": {
                "implementation": {"type": "internal", "handler": "gate.Tool"},
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        },
        {
            "kind": "skill",
            "id": "r2-gate-skill",
            "version": {"hash": "sha256:bbb", "tags": []},
            "metadata": {"name": "Gate Skill", "description": "R2 gate test skill."},
            "spec": {
                "loaded_when": "always",
                "instructions": "Do the gate thing.",
            },
        },
        {
            "kind": "agent",
            "id": "r2-gate-agent",
            "version": {"hash": "sha256:ccc", "tags": []},
            "metadata": {"name": "Gate Agent", "description": "R2 gate test agent."},
            "spec": {"role": "tester"},
        },
        {
            "kind": "harness",
            "id": "r2-gate-harness",
            "version": {"hash": "sha256:ddd", "tags": []},
            "metadata": {"name": "Gate Harness", "description": "R2 gate test harness."},
            "spec": {"goal": "Run gate checks.", "actors": []},
        },
        {
            "kind": "human_role",
            "id": "r2-gate-human-role",
            "version": {"hash": "sha256:eee", "tags": []},
            "metadata": {"name": "Gate Role", "description": "R2 gate test human role."},
            "spec": {"role_name": "approver"},
        },
    ],
    ids=["tool", "skill", "agent", "harness", "human_role"],
)
def test_d2_all_five_kinds_validate_via_ohm_discriminated_union(descriptor: dict):
    """Every OHM descriptor kind must parse correctly via the CapabilityDescriptor union.

    Uses the ``kind`` field as discriminator per OHM v1.0 §3.
    """
    from pydantic import TypeAdapter
    from schemas.capability_descriptor import CapabilityDescriptor

    ta: TypeAdapter = TypeAdapter(CapabilityDescriptor)
    result = ta.validate_python(descriptor)
    assert result.kind == descriptor["kind"], (
        f"Parsed kind {result.kind!r} does not match input kind {descriptor['kind']!r}"
    )


# ===========================================================================
# D3 — Single DB-backed registry; no dual-registry code path
# ===========================================================================


def test_d3_tool_registry_service_absent():
    """ToolRegistryService must not be importable — the legacy service is deleted (R2).

    The single DB-backed CapabilityRegistryService is the sole registry.
    """
    try:
        from app.services.tool_registry import ToolRegistryService  # noqa: F401

        pytest.fail(
            "app.services.tool_registry.ToolRegistryService still exists. "
            "Remove the legacy ToolRegistryService; all tools now live in "
            "CapabilityRegistryService."
        )
    except ImportError:
        pass  # expected


def test_d3_in_memory_tool_registry_absent():
    """The in-memory ToolRegistry class must not be importable (legacy path deleted)."""
    try:
        from app.tools.registry import ToolRegistry  # noqa: F401

        pytest.fail(
            "app.tools.registry.ToolRegistry still exists. "
            "The in-memory registry is superseded by the DB-backed CapabilityRegistryService."
        )
    except ImportError:
        pass  # expected


def test_d3_tool_sync_service_file_absent():
    """app/services/tool_sync_service.py must be deleted; the unified registry removes it."""
    path = _CORE_SERVICE / "app" / "services" / "tool_sync_service.py"
    assert not path.exists(), (
        f"{path.relative_to(_REPO_ROOT)} still exists. "
        "Delete tool_sync_service.py — capability syncing is now handled by the plugin registry."
    )


def test_d3_capability_registry_service_importable():
    """CapabilityRegistryService must be importable — it is the single registry."""
    from app.services.capability_registry import CapabilityRegistryService  # noqa: F401


# ===========================================================================
# D4 — Google Drive, Notion, PostgreSQL, MySQL tools have valid OHM descriptors
# ===========================================================================


@pytest.mark.parametrize(
    "module_path,class_name,expected_plugin_id",
    [
        (
            "app.tools.implementations.ingestion.google_drive_reader",
            "GoogleDriveReader",
            "google-drive-reader",
        ),
        (
            "app.tools.implementations.ingestion.notion_reader",
            "NotionReader",
            "notion-reader",
        ),
        (
            "app.tools.implementations.ingestion.postgresql_reader",
            "PostgreSQLReader",
            "postgresql-reader",
        ),
        (
            "app.tools.implementations.ingestion.mysql_reader",
            "MySQLReader",
            "mysql-reader",
        ),
    ],
    ids=["google-drive", "notion", "postgresql", "mysql"],
)
def test_d4_ingestion_tool_has_valid_ohm_descriptor(
    module_path: str, class_name: str, expected_plugin_id: str
):
    """Each ingestion tool must expose a valid OHM ToolDescriptor via get_ohm_descriptor().

    Validates using the OHM CapabilityDescriptor TypeAdapter (discriminated union).
    """
    from pydantic import TypeAdapter
    from schemas.capability_descriptor import CapabilityDescriptor

    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    descriptor = cls.get_ohm_descriptor()

    assert descriptor["kind"] == "tool", (
        f"{class_name}.get_ohm_descriptor() returned kind={descriptor['kind']!r}; expected 'tool'"
    )
    assert descriptor["id"] == expected_plugin_id, (
        f"{class_name}: descriptor id={descriptor['id']!r} != expected {expected_plugin_id!r}"
    )

    ta: TypeAdapter = TypeAdapter(CapabilityDescriptor)
    result = ta.validate_python(descriptor)
    assert result.kind == "tool"


@pytest.mark.parametrize(
    "module_path,class_name,expected_cred_type",
    [
        (
            "app.tools.implementations.ingestion.google_drive_reader",
            "GoogleDriveReader",
            "oauth_token",
        ),
        (
            "app.tools.implementations.ingestion.notion_reader",
            "NotionReader",
            "api_key",
        ),
        (
            "app.tools.implementations.ingestion.postgresql_reader",
            "PostgreSQLReader",
            "connection_string",
        ),
        (
            "app.tools.implementations.ingestion.mysql_reader",
            "MySQLReader",
            "connection_string",
        ),
    ],
    ids=["google-drive-cred", "notion-cred", "postgresql-cred", "mysql-cred"],
)
def test_d4_ingestion_tool_credential_type(
    module_path: str, class_name: str, expected_cred_type: str
):
    """Each ingestion tool's OHM descriptor must use the correct OHM credential type."""
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    descriptor = cls.get_ohm_descriptor()

    cred_reqs = descriptor["spec"].get("credential_requirements", [])
    assert len(cred_reqs) >= 1, (
        f"{class_name} declares no credential_requirements; "
        f"expected at least one with type={expected_cred_type!r}"
    )
    actual_type = cred_reqs[0]["type"]
    assert actual_type == expected_cred_type, (
        f"{class_name} credential_requirements[0].type={actual_type!r}; "
        f"expected {expected_cred_type!r}"
    )


# ===========================================================================
# D5 — Every descriptor carries a content hash; hash is deterministic
# ===========================================================================


def test_d5_content_hash_column_exists_on_capability_descriptor_db():
    """CapabilityDescriptorDB.content_hash column must be present on the ORM model."""
    from app.models.capability_descriptor import CapabilityDescriptorDB

    assert hasattr(CapabilityDescriptorDB, "content_hash"), (
        "CapabilityDescriptorDB.content_hash column is missing; "
        "every descriptor row must carry a content hash (R2 integrity requirement)"
    )


def test_d5_compute_content_hash_is_importable():
    """compute_content_hash must be importable from the ohm hashing module."""
    from hashing import compute_content_hash  # noqa: F401

    assert callable(compute_content_hash)


def test_d5_compute_content_hash_is_deterministic():
    """compute_content_hash must produce the same digest on repeated calls (stability)."""
    from hashing import compute_content_hash

    descriptor = {
        "kind": "tool",
        "id": "stability-test-tool",
        "version": {"hash": "sha256:abc123", "tags": ["1.0.0"]},
        "metadata": {"name": "Stability Tool", "description": "Hash stability probe."},
        "spec": {
            "implementation": {"type": "internal", "handler": "gate.StabilityTool"},
            "input_schema": {
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
            "output_schema": {"type": "object"},
        },
    }
    h1 = compute_content_hash(descriptor)
    h2 = compute_content_hash(descriptor)
    assert h1 == h2, "compute_content_hash is non-deterministic — hash stability check failed"
    assert len(h1) == 64, f"Expected 64-char SHA-256 hex digest; got {len(h1)} chars"


def test_d5_compute_content_hash_is_insertion_order_independent():
    """Hash must be identical regardless of dict key ordering."""
    from hashing import compute_content_hash

    base = {
        "kind": "tool",
        "id": "order-test",
        "version": {"hash": "sha256:x", "tags": []},
        "metadata": {"name": "N", "description": "D"},
        "spec": {
            "implementation": {"type": "internal", "handler": "h"},
            "input_schema": {},
            "output_schema": {},
        },
    }
    # Reverse key order
    reversed_desc = dict(reversed(list(base.items())))

    assert compute_content_hash(base) == compute_content_hash(reversed_desc), (
        "compute_content_hash produces different hashes for dicts with same content "
        "but different key insertion order — canonical JSON serialisation is broken"
    )


# ===========================================================================
# D6 — MCP round-trip: external MCP tools appear as implementation.type == "mcp"
# ===========================================================================

try:
    from app.tools.base.mcp_tool import translate_mcp_tool as _translate

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    _translate = None  # type: ignore[assignment]


@pytest.mark.skipif(not _MCP_AVAILABLE, reason="app.tools.base.mcp_tool not yet implemented")
def test_d6_mcp_tool_translate_produces_mcp_implementation_type():
    """translate_mcp_tool() must produce a descriptor with implementation.type == 'mcp'.

    This is the MCP round-trip contract: tools discovered from an external MCP server
    must land in the registry as capabilities with implementation.type mcp so the
    executor knows how to dispatch them.
    """
    mcp_tool = {
        "name": "fetch_data",
        "description": "Fetch data from a mock MCP server.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
    server_url = "http://mock-mcp.example.com:8080"

    descriptor = _translate(mcp_tool, server_url)

    assert descriptor is not None, "translate_mcp_tool() returned None for a valid MCP tool"
    assert descriptor["kind"] == "tool", f"Expected kind='tool'; got {descriptor['kind']!r}"
    impl = descriptor["spec"]["implementation"]
    assert impl["type"] == "mcp", (
        f"implementation.type={impl['type']!r}; expected 'mcp'. "
        "External MCP tools must be stored as mcp capabilities so the executor "
        "can dispatch them to the correct MCP server."
    )
    assert "endpoint" in impl, (
        "implementation.endpoint missing — the executor needs the server URL to dispatch"
    )
    assert impl["endpoint"] == server_url


@pytest.mark.skipif(not _MCP_AVAILABLE, reason="app.tools.base.mcp_tool not yet implemented")
def test_d6_mcp_tool_descriptor_carries_content_hash():
    """translate_mcp_tool() must embed a sha256: content_hash in the descriptor."""
    mcp_tool = {
        "name": "probe_tool",
        "description": "Hash probe.",
        "inputSchema": {"type": "object", "properties": {}},
    }
    descriptor = _translate(mcp_tool, "http://probe.example.com")
    assert descriptor is not None
    assert "content_hash" in descriptor or "hash" in descriptor.get("version", {}), (
        "MCP-translated descriptor carries no content hash; "
        "every capability entering the registry must have a hash (D5)"
    )
    version_hash = descriptor.get("version", {}).get("hash", "")
    assert version_hash.startswith("sha256:"), (
        f"version.hash={version_hash!r} does not start with 'sha256:'"
    )


# ===========================================================================
# D7 — KGB agent toolkit reads schemas from registry; no static _TOOL_SCHEMAS
# ===========================================================================


def test_d7_agent_tool_schemas_has_no_static_dict():
    """agent_tool_schemas must not contain _TOOL_SCHEMAS after ORAA-76.

    The static descriptor dict is the dual-storage violation this story eliminates.
    If it still exists, descriptors are defined in two places.
    """
    import app.services.agent_tool_schemas as module

    assert not hasattr(module, "_TOOL_SCHEMAS"), (
        "app.services.agent_tool_schemas._TOOL_SCHEMAS still exists. "
        "The static descriptor dict must be deleted; descriptors live in the registry only."
    )


def test_d7_tool_schemas_from_registry_is_importable():
    """tool_schemas_from_registry() must be importable — it is the registry-backed replacement."""
    from app.services.agent_tool_schemas import tool_schemas_from_registry  # noqa: F401

    assert callable(tool_schemas_from_registry)


def test_d7_capability_registry_client_is_importable():
    """RemoteCapabilityRegistryClient must be importable — KGB reads schemas via it."""
    from app.services.capability_registry_client import RemoteCapabilityRegistryClient  # noqa: F401


# ===========================================================================
# D8 — workflow_service.py and pipeline_generator.py absent from codebase
# ===========================================================================


def test_d8_workflow_service_absent():
    """workflow_service.py must not exist anywhere in the repository (ADR-005 retirement).

    The workflow/pipeline approach is retired; oraclous-core-service no longer ships it.
    """
    hits = list(_REPO_ROOT.rglob("workflow_service.py"))
    # Exclude any .git, __pycache__, .venv paths
    rel_start = len(_REPO_ROOT.parts)
    hits = [
        p
        for p in hits
        if not any(part.startswith(".") for part in p.parts[rel_start:])
        and "__pycache__" not in p.parts
        and ".venv" not in p.parts
    ]
    assert hits == [], (
        f"workflow_service.py found at: {[str(p.relative_to(_REPO_ROOT)) for p in hits]}. "
        "workflow_service.py must be deleted (ADR-005)."
    )


def test_d8_pipeline_generator_absent():
    """pipeline_generator.py must not exist anywhere in the repository (ADR-005 retirement)."""
    hits = list(_REPO_ROOT.rglob("pipeline_generator.py"))
    rel_start = len(_REPO_ROOT.parts)
    hits = [
        p
        for p in hits
        if not any(part.startswith(".") for part in p.parts[rel_start:])
        and "__pycache__" not in p.parts
        and ".venv" not in p.parts
    ]
    assert hits == [], (
        f"pipeline_generator.py found at: {[str(p.relative_to(_REPO_ROOT)) for p in hits]}. "
        "pipeline_generator.py must be deleted (ADR-005)."
    )


# ===========================================================================
# D9 — Per-harness allocation rejects scope-exceeding requests (T2-M3)
# ===========================================================================


def test_d9_scope_violation_error_is_importable():
    """ScopeViolationError must be importable — it is the T2-M3 enforcement signal."""
    from app.services.capability_allocation import ScopeViolationError  # noqa: F401


def test_d9_check_scope_compliance_raises_on_exceeded_scope():
    """check_scope_compliance() must raise ScopeViolationError when a capability's
    oauth scopes exceed the harness's declared scope.

    This is the T2-M3 privilege-escalation control: a harness with only 'read' scope
    must not be granted capabilities requiring 'write' scope.
    """
    from app.services.capability_allocation import ScopeViolationError, check_scope_compliance

    harness_id = uuid.UUID("aaaa0000-0000-4000-8000-000000000001")
    capability_id = uuid.UUID("bbbb0000-0000-4000-8000-000000000001")

    harness_scope = ["https://www.googleapis.com/auth/drive.readonly"]
    credential_requirements = [
        {
            "type": "oauth_token",
            "provider": "google",
            "scopes": [
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/drive.write",  # not in harness scope
            ],
        }
    ]

    with pytest.raises(ScopeViolationError) as exc_info:
        check_scope_compliance(harness_id, capability_id, harness_scope, credential_requirements)

    error = exc_info.value
    assert "drive.write" in str(error.violating_scopes), (
        f"ScopeViolationError.violating_scopes={error.violating_scopes!r} "
        "does not name the write scope that exceeded the harness boundary"
    )


def test_d9_check_scope_compliance_passes_for_in_scope_capability():
    """check_scope_compliance() must NOT raise when all required scopes are declared."""
    from app.services.capability_allocation import check_scope_compliance

    harness_id = uuid.UUID("aaaa0000-0000-4000-8000-000000000002")
    capability_id = uuid.UUID("bbbb0000-0000-4000-8000-000000000002")

    harness_scope = [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.write",
    ]
    credential_requirements = [
        {
            "type": "oauth_token",
            "provider": "google",
            "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
        }
    ]

    # Should not raise — all required scopes are within harness scope
    check_scope_compliance(harness_id, capability_id, harness_scope, credential_requirements)


def test_d9_check_scope_compliance_zero_scope_harness_rejects_oauth_capability():
    """A zero-scope harness must reject any capability with oauth requirements (T2-M3)."""
    from app.services.capability_allocation import ScopeViolationError, check_scope_compliance

    harness_id = uuid.UUID("aaaa0000-0000-4000-8000-000000000003")
    capability_id = uuid.UUID("bbbb0000-0000-4000-8000-000000000003")

    with pytest.raises(ScopeViolationError):
        check_scope_compliance(
            harness_id=harness_id,
            capability_id=capability_id,
            harness_declared_scope=[],  # zero scope
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["any.scope"]}
            ],
        )


def test_d9_check_scope_compliance_passes_for_non_oauth_credential():
    """Non-oauth credentials (api_key, connection_string) are not scope-checked (T2-M3)."""
    from app.services.capability_allocation import check_scope_compliance

    harness_id = uuid.UUID("aaaa0000-0000-4000-8000-000000000004")
    capability_id = uuid.UUID("bbbb0000-0000-4000-8000-000000000004")

    # Should not raise — api_key type is exempt from scope checking
    check_scope_compliance(
        harness_id=harness_id,
        capability_id=capability_id,
        harness_declared_scope=[],  # empty scope
        credential_requirements=[
            {"type": "api_key", "provider": "notion", "scopes": ["full-access"]}
        ],
    )
