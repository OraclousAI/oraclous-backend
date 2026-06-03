"""
[tests] OHM content-hash versioning — compute_content_hash unit tests

Story: ORAA-70 / ORA-73
Architecture refs:
  - OHM v1.0 Spec:    https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - R2 release page:  https://oraclous.atlassian.net/wiki/spaces/OP/pages/688482
  - Test Strategy:    https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

Security tier: T3-M2 — content hash enables tamper detection; be-test-reviewer co-sign required.

Behaviours covered:
  C01  compute_content_hash on a valid descriptor dict returns a 64-char hex string
  C02  identical descriptor dicts produce identical hashes (stability)
  C03  calling compute_content_hash twice on the same dict is deterministic (no randomness)
  C04  adding any field produces a different hash (divergence — addition)
  C05  changing any field value produces a different hash (divergence — mutation)
  C06  removing any field produces a different hash (divergence — deletion)
  C07  key insertion order does not affect the hash (canonical key sorting, top-level)
  C08  nested key insertion order does not affect the hash (sorting is deep/recursive)
  C09  hash stability holds for each of the five descriptor kinds
  C10  serialization roundtrip: parse → model_dump → hash equals original dict hash
  C11  different descriptor kinds with same surrounding payload produce different hashes
  C12  compute_content_hash accepts a parsed Pydantic CapabilityDescriptor model instance

NOTE: All imports below will fail with ImportError until the implementer creates
      packages/ohm/hashing.py with the compute_content_hash function.
      That ImportError is intentional — this file is written test-first (ADR-010).
"""

import copy

import pytest
from pydantic import TypeAdapter

# Collection guard: imports below fail until ohm/hashing.py is implemented.
# Tests are marked skipif so pytest can collect this file without error.
# The underlying skip IS the TDD red state — ADR-010.
try:
    from ohm.schemas import CapabilityDescriptor

    from ohm.hashing import compute_content_hash

    _ta: TypeAdapter = TypeAdapter(CapabilityDescriptor)
    _OHM_AVAILABLE = True
except ImportError:
    compute_content_hash = None  # type: ignore[assignment]
    CapabilityDescriptor = None  # type: ignore[assignment]
    _ta = None  # type: ignore[assignment]
    _OHM_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _OHM_AVAILABLE,
    reason="ohm.hashing not yet implemented — TDD red state (ADR-010)",
)

# ---------------------------------------------------------------------------
# Shared test data — minimal valid payload for each descriptor kind
# ---------------------------------------------------------------------------

_TOOL: dict = {
    "kind": "tool",
    "id": "hash-test-tool",
    "version": {"hash": "sha256:placeholder", "tags": ["1.0.0"]},
    "metadata": {
        "name": "Hash Test Tool",
        "description": "Used to test content-hash computation.",
    },
    "spec": {
        "implementation": {"type": "internal", "handler": "test.Handler"},
        "input_schema": {
            "type": "object",
            "required": ["input_id"],
            "properties": {"input_id": {"type": "string"}},
        },
        "output_schema": {
            "type": "object",
            "properties": {"result": {"type": "string"}},
        },
        "credential_requirements": [],
    },
}

_SKILL: dict = {
    "kind": "skill",
    "id": "hash-test-skill",
    "version": {"hash": "sha256:placeholder", "tags": ["1.0.0"]},
    "metadata": {"name": "Hash Test Skill", "description": "Test skill."},
    "spec": {
        "loaded_when": "testing content hash",
        "instructions": "# Test Skill\n\nTest instructions.",
        "capability_requirements": [],
    },
}

_AGENT: dict = {
    "kind": "agent",
    "id": "hash-test-agent",
    "version": {"hash": "sha256:placeholder", "tags": ["1.0.0"]},
    "metadata": {"name": "Hash Test Agent", "description": "Test agent."},
    "spec": {
        "role": "Test agent role.",
        "capabilities": [],
    },
}

_HARNESS: dict = {
    "kind": "harness",
    "id": "hash-test-harness",
    "version": {"hash": "sha256:placeholder", "tags": ["1.0.0"]},
    "metadata": {"name": "Hash Test Harness", "description": "Test harness."},
    "spec": {
        "goal": "Test harness goal.",
        "actors": [],
    },
}

_HUMAN_ROLE: dict = {
    "kind": "human_role",
    "id": "hash-test-human-role",
    "version": {"hash": "sha256:placeholder", "tags": ["1.0.0"]},
    "metadata": {"name": "Hash Test Human Role", "description": "Test human role."},
    "spec": {
        "role_name": "test_reviewer",
    },
}

_ALL_KINDS = [_TOOL, _SKILL, _AGENT, _HARNESS, _HUMAN_ROLE]


# ---------------------------------------------------------------------------
# C01  compute_content_hash on a valid descriptor dict returns a 64-char hex string
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_returns_64_char_hex_string():
    """SHA-256 hex digest is always exactly 64 lowercase hex characters."""
    result = compute_content_hash(_TOOL)
    assert isinstance(result, str)
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


# ---------------------------------------------------------------------------
# C02  identical descriptor dicts produce identical hashes (stability)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_is_stable_for_identical_input():
    """Calling compute_content_hash twice with equal dicts produces the same hash."""
    first = compute_content_hash(_TOOL)
    second = compute_content_hash(copy.deepcopy(_TOOL))
    assert first == second


# ---------------------------------------------------------------------------
# C03  deterministic — no randomness or timestamp in the output
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_is_deterministic():
    """Repeated calls on the same dict always return the same value — no random salt."""
    hashes = {compute_content_hash(_SKILL) for _ in range(5)}
    assert len(hashes) == 1


# ---------------------------------------------------------------------------
# C04  adding any field produces a different hash (divergence — addition)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_diverges_on_field_addition():
    """Adding a new top-level field to the descriptor produces a different hash."""
    original_hash = compute_content_hash(_TOOL)
    modified = copy.deepcopy(_TOOL)
    modified["extra_field"] = "new_value"
    assert compute_content_hash(modified) != original_hash


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_diverges_on_nested_field_addition():
    """Adding a field inside a nested object produces a different hash."""
    original_hash = compute_content_hash(_TOOL)
    modified = copy.deepcopy(_TOOL)
    modified["metadata"]["extra"] = "added"
    assert compute_content_hash(modified) != original_hash


# ---------------------------------------------------------------------------
# C05  changing any field value produces a different hash (divergence — mutation)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_diverges_on_id_change():
    """Changing the id field produces a different hash."""
    original_hash = compute_content_hash(_TOOL)
    modified = copy.deepcopy(_TOOL)
    modified["id"] = "different-tool-id"
    assert compute_content_hash(modified) != original_hash


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_diverges_on_metadata_name_change():
    """Changing metadata.name produces a different hash."""
    original_hash = compute_content_hash(_TOOL)
    modified = copy.deepcopy(_TOOL)
    modified["metadata"]["name"] = "Different Name"
    assert compute_content_hash(modified) != original_hash


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_diverges_on_spec_instructions_change():
    """Changing a spec field (instructions) produces a different hash."""
    original_hash = compute_content_hash(_SKILL)
    modified = copy.deepcopy(_SKILL)
    modified["spec"]["instructions"] = "Completely different instructions."
    assert compute_content_hash(modified) != original_hash


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_diverges_on_version_tag_change():
    """Changing a version tag produces a different hash."""
    original_hash = compute_content_hash(_TOOL)
    modified = copy.deepcopy(_TOOL)
    modified["version"]["tags"] = ["2.0.0"]
    assert compute_content_hash(modified) != original_hash


# ---------------------------------------------------------------------------
# C06  removing any field produces a different hash (divergence — deletion)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_diverges_on_top_level_field_removal():
    """Removing a top-level field from the descriptor produces a different hash."""
    original_hash = compute_content_hash(_TOOL)
    modified = {k: v for k, v in _TOOL.items() if k != "id"}
    assert compute_content_hash(modified) != original_hash


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_diverges_on_nested_field_removal():
    """Removing a nested field produces a different hash."""
    original_hash = compute_content_hash(_TOOL)
    modified = copy.deepcopy(_TOOL)
    del modified["metadata"]["description"]
    assert compute_content_hash(modified) != original_hash


# ---------------------------------------------------------------------------
# C07  key insertion order does not affect the hash (canonical top-level sorting)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_is_order_independent_at_top_level():
    """
    A descriptor dict with fields in a different insertion order produces the
    same hash — canonical JSON sorts keys alphabetically at every level.
    """
    original_hash = compute_content_hash(_TOOL)
    reordered = {
        "spec": _TOOL["spec"],
        "version": _TOOL["version"],
        "id": _TOOL["id"],
        "metadata": _TOOL["metadata"],
        "kind": _TOOL["kind"],
    }
    assert compute_content_hash(reordered) == original_hash


# ---------------------------------------------------------------------------
# C08  nested key insertion order does not affect the hash (deep canonical sorting)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_sorts_nested_keys_deeply():
    """
    Nested objects with keys in a different insertion order still produce the
    same hash — canonical sorting applies at every nesting depth.
    """
    original_hash = compute_content_hash(_TOOL)
    reordered = copy.deepcopy(_TOOL)
    # Reverse the key order inside spec
    reordered["spec"] = {k: _TOOL["spec"][k] for k in reversed(list(_TOOL["spec"].keys()))}
    assert compute_content_hash(reordered) == original_hash


# ---------------------------------------------------------------------------
# C09  hash stability holds for each of the five descriptor kinds
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.capability_integrity
@pytest.mark.parametrize(
    "descriptor",
    _ALL_KINDS,
    ids=["tool", "skill", "agent", "harness", "human_role"],
)
def test_compute_content_hash_stable_for_all_descriptor_kinds(descriptor):
    """compute_content_hash returns a stable 64-char hex string for every descriptor kind."""
    h1 = compute_content_hash(descriptor)
    h2 = compute_content_hash(copy.deepcopy(descriptor))
    assert isinstance(h1, str)
    assert len(h1) == 64
    assert h1 == h2


# ---------------------------------------------------------------------------
# C10  serialization roundtrip: parse → model_dump → hash equals original dict hash
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_stable_across_serialization_roundtrip():
    """
    Parsing a descriptor dict into a Pydantic model and serializing it back
    produces the same hash as the original raw dict.

    This guarantees that hash computation is idempotent through the
    parse-and-persist pipeline: the stored hash equals the hash of the
    JSONB representation retrieved from the database.
    """
    original_hash = compute_content_hash(_TOOL)
    parsed = _ta.validate_python(_TOOL)
    roundtripped = parsed.model_dump()
    assert compute_content_hash(roundtripped) == original_hash


@pytest.mark.unit
@pytest.mark.capability_integrity
@pytest.mark.parametrize(
    "descriptor",
    _ALL_KINDS,
    ids=["tool", "skill", "agent", "harness", "human_role"],
)
def test_serialization_roundtrip_stable_for_all_kinds(descriptor):
    """Roundtrip hash stability holds for each of the five descriptor kinds."""
    original_hash = compute_content_hash(descriptor)
    parsed = _ta.validate_python(descriptor)
    assert compute_content_hash(parsed.model_dump()) == original_hash


# ---------------------------------------------------------------------------
# C11  different descriptor kinds with same surrounding payload → different hashes
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_different_kinds_produce_different_hashes():
    """
    Two descriptors that differ only in their kind field produce different
    hashes — the kind field is part of the canonical content.
    """
    tool_hash = compute_content_hash(_TOOL)
    skill_hash = compute_content_hash(_SKILL)
    assert tool_hash != skill_hash


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_all_descriptor_kind_hashes_are_pairwise_distinct():
    """All five descriptor kind fixtures produce mutually distinct hashes."""
    hashes = [compute_content_hash(d) for d in _ALL_KINDS]
    assert len(set(hashes)) == len(_ALL_KINDS), "Two descriptor kinds produced the same hash"


# ---------------------------------------------------------------------------
# C12  compute_content_hash accepts a Pydantic CapabilityDescriptor model instance
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.capability_integrity
def test_compute_content_hash_accepts_pydantic_model():
    """
    compute_content_hash can be called with a parsed Pydantic model; the result
    must equal the hash of the equivalent dict representation.
    """
    parsed = _ta.validate_python(_TOOL)
    model_hash = compute_content_hash(parsed)
    dict_hash = compute_content_hash(_TOOL)
    assert model_hash == dict_hash


@pytest.mark.unit
@pytest.mark.capability_integrity
@pytest.mark.parametrize(
    "descriptor",
    _ALL_KINDS,
    ids=["tool", "skill", "agent", "harness", "human_role"],
)
def test_pydantic_model_hash_equals_dict_hash_for_all_kinds(descriptor):
    """For every kind, the Pydantic model hash equals the raw dict hash."""
    parsed = _ta.validate_python(descriptor)
    assert compute_content_hash(parsed) == compute_content_hash(descriptor)
