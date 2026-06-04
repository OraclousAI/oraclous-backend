"""Tests proving the organisation-scoping guardrails fire (ORA-10 / 0b)."""

from pathlib import Path

import pytest
from tools.lint.check_org_scoping import check_source

pytestmark = pytest.mark.unit


def _rules(src: str) -> set[str]:
    return {v.rule for v in check_source(src)}


def test_org001_subscript_from_body() -> None:
    src = "def handler(body):\n    org = body['organisation_id']\n    return org\n"
    assert "ORG001" in _rules(src)


def test_org001_attribute_from_payload() -> None:
    src = "def handler(payload):\n    return payload.organization_id\n"
    assert "ORG001" in _rules(src)


def test_org001_pydantic_request_model_input_field() -> None:
    # A genuine inbound Pydantic request schema declaring organisation_id is flagged.
    src = "class CreateThingRequest(BaseModel):\n    name: str\n    organisation_id: str\n"
    assert "ORG001" in _rules(src)


def test_org001_non_pydantic_request_dataclass_not_flagged() -> None:
    # A plain domain value object named *Request that carries organisation_id
    # through a seam is not an inbound body schema, so it is not flagged (ORA-40).
    src = (
        "@dataclass(frozen=True, slots=True)\n"
        "class AccessRequest:\n"
        "    organisation_id: str\n"
        "    subject: str\n"
    )
    assert "ORG001" not in _rules(src)


def test_org001_attribute_from_body_still_flagged() -> None:
    src = "def handler(body):\n    return body.organisation_id\n"
    assert "ORG001" in _rules(src)


def test_org001_attribute_from_request_domain_object_not_flagged() -> None:
    # `request`/`req`/`data` routinely name domain objects; an attribute read off
    # them is not body trust (the rebac.py:64 pattern). ORA-40 / security-architect.
    src = "def check(request):\n    return request.organisation_id\n"
    assert "ORG001" not in _rules(src)


def test_org001_subscript_from_request_still_flagged() -> None:
    # Dict-style extraction stays broad — subscripting a domain object is not a real
    # pattern, so request["organisation_id"] is still treated as untrusted body trust.
    src = "def handler(request):\n    return request['organisation_id']\n"
    assert "ORG001" in _rules(src)


def test_org001_substrate_rebac_patterns_clean() -> None:
    # Mirrors packages/substrate/.../rebac.py:23 (AccessRequest dataclass) and
    # :64 (the organisation_id presence-validation that enforces ADR-006).
    src = (
        "@dataclass(frozen=True, slots=True)\n"
        "class AccessRequest:\n"
        "    organisation_id: str\n"
        "    subject: str\n"
        "    resource: str\n"
        "    relation: str\n"
        "\n"
        "def check(request):\n"
        "    if not request.organisation_id or not request.organisation_id.strip():\n"
        "        raise ValueError('organisation_id is required')\n"
    )
    assert "ORG001" not in _rules(src)


def test_org002_storage_model_without_org_is_flagged() -> None:
    src = "class Thing(Base):\n    __tablename__ = 'things'\n    id = Column(Integer)\n"
    assert "ORG002" in _rules(src)


def test_org002_storage_model_with_org_passes() -> None:
    src = (
        "class Thing(Base):\n"
        "    __tablename__ = 'things'\n"
        "    organisation_id = Column(UUID)\n"
        "    id = Column(Integer)\n"
    )
    assert "ORG002" not in _rules(src)


def test_org002_cross_org_principal_marker_exempts_identity_table() -> None:
    # a cross-org principal table (e.g. users) opts out: org is on the token + membership, not a row
    src = (
        "class User(Base):\n"
        "    '''A human user. org-scoping: cross-org-principal — org via membership.'''\n"
        "    __tablename__ = 'users'\n"
        "    id = Column(String)\n"
    )
    assert "ORG002" not in _rules(src)


def test_org002_without_marker_still_flagged() -> None:
    # the exemption is opt-in only — an unmarked org-less table is still flagged
    src = "class Untenanted(Base):\n    __tablename__ = 'untenanted'\n    id = Column(String)\n"
    assert "ORG002" in _rules(src)


def test_clean_source_has_no_violations() -> None:
    assert _rules("def add(a, b):\n    return a + b\n") == set()


# --- ORA-41: Neo4j label DDL (ORG003) -------------------------------------------


def test_org003_org_scoped_label_index_without_org_is_flagged() -> None:
    # A hardcoded index on an org-scoped label whose ON clause omits org.
    src = "Q = 'CREATE INDEX entity_idx IF NOT EXISTS FOR (n:`__Entity__`) ON (n.graph_id)'\n"
    assert "ORG003" in _rules(src)


def test_org003_org_scoped_label_index_with_org_passes() -> None:
    src = (
        "Q = 'CREATE INDEX entity_idx IF NOT EXISTS "
        "FOR (n:`__Entity__`) ON (n.organisation_id, n.graph_id)'\n"
    )
    assert "ORG003" not in _rules(src)


def test_org003_canonical_loop_idiom_with_interpolated_org_passes() -> None:
    # Mirrors schema/neo4j.py apply(): the label is interpolated from the
    # ORG_SCOPED_LABELS loop and org is the interpolated ORG_PROPERTY constant.
    src = (
        "ORG_PROPERTY = 'organisation_id'\n"
        "def apply(driver):\n"
        "    for label in ('__Entity__', 'Chunk'):\n"
        "        driver.execute_query(\n"
        "            f'CREATE INDEX {label}_idx IF NOT EXISTS '\n"
        "            f'FOR (n:`{label}`) ON (n.{ORG_PROPERTY}, n.graph_id)'\n"
        "        )\n"
    )
    assert "ORG003" not in _rules(src)


def test_org003_loop_idiom_dropping_org_is_flagged() -> None:
    # The drift the compensating control exists to catch: loops over the
    # org-scoped labels but the ON clause no longer carries org.
    src = (
        "def apply(driver):\n"
        "    for label in ('__Entity__', 'Chunk'):\n"
        "        driver.execute_query(\n"
        "            f'CREATE INDEX {label}_idx IF NOT EXISTS FOR (n:`{label}`) ON (n.graph_id)'\n"
        "        )\n"
    )
    assert "ORG003" in _rules(src)


def test_org003_non_org_scoped_label_index_not_flagged() -> None:
    # An index on a label that is not in the org-scoped set is out of ORG003 scope.
    src = "Q = 'CREATE INDEX audit_idx IF NOT EXISTS FOR (n:`AuditLog`) ON (n.created_at)'\n"
    assert "ORG003" not in _rules(src)


def test_org003_ddl_in_docstring_not_flagged() -> None:
    # Prose mentioning DDL in a docstring is not executable schema; must not flag.
    src = (
        '"""Indexes are created via CREATE INDEX ... FOR (n:`__Entity__`) ON (n.graph_id)."""\n'
        "x = 1\n"
    )
    assert "ORG003" not in _rules(src)


# --- ORA-41: Redis qcache key prefix (ORG004) -----------------------------------


def test_org004_qcache_key_without_org_outer_segment_is_flagged() -> None:
    # The legacy graph-only key shape — first segment after the prefix is graph_id.
    src = "def key(graph_id, sha):\n    return f'qcache:{graph_id}:{sha}'\n"
    assert "ORG004" in _rules(src)


def test_org004_qcache_key_with_org_outer_segment_passes() -> None:
    src = (
        "_PREFIX = 'qcache'\n"
        "def key(org, graph, digest):\n"
        "    return f'{_PREFIX}:{org}:{graph}:{digest}'\n"
    )
    assert "ORG004" not in _rules(src)


def test_org004_qcache_org_scoped_pattern_passes() -> None:
    src = "_PREFIX = 'qcache'\ndef pattern(org):\n    return f'{_PREFIX}:{org}:*'\n"
    assert "ORG004" not in _rules(src)


def test_org004_pure_wildcard_namespace_scan_is_exempt() -> None:
    # The migration's cold-start SCAN over the whole namespace (f"{_PREFIX}:*")
    # is a maintenance glob, not a per-tenant key write — exempt by design.
    src = (
        "_CACHE_PREFIX = 'qcache'\n"
        "def migrate(redis):\n"
        "    redis.scan(match=f'{_CACHE_PREFIX}:*')\n"
    )
    assert "ORG004" not in _rules(src)


def test_org004_global_opt_out_marker_suppresses_flag() -> None:
    # A deliberately-global key must say so explicitly rather than be silently bypassed.
    src = "def health_key():\n    return f'qcache:health:{node}'  # org-scoping: global\n"
    assert "ORG004" not in _rules(src)


def test_org004_test_style_org_constant_segment_passes() -> None:
    # Mirrors existing test f-strings (f"qcache:{ORG_A}:{GRAPH}:") — an org-named
    # constant in the outer segment is recognised as org scope; must not flag.
    src = "ORG_A = 'a'\nGRAPH = 'g'\nk = f'qcache:{ORG_A}:{GRAPH}:'\n"
    assert "ORG004" not in _rules(src)


# --- ORA-41: vector / fulltext index DDL (ORG005) -------------------------------


def test_org005_vector_index_without_org_is_flagged() -> None:
    src = (
        "Q = 'CREATE VECTOR INDEX chunk_vec IF NOT EXISTS "
        "FOR (n:Chunk) ON (n.embedding) OPTIONS {}'\n"
    )
    assert "ORG005" in _rules(src)


def test_org005_fulltext_index_without_org_is_flagged() -> None:
    src = "Q = 'CREATE FULLTEXT INDEX chunk_ft IF NOT EXISTS FOR (n:Chunk) ON EACH [n.text]'\n"
    assert "ORG005" in _rules(src)


def test_org005_fulltext_index_with_org_property_passes() -> None:
    src = (
        "Q = 'CREATE FULLTEXT INDEX chunk_ft IF NOT EXISTS "
        "FOR (n:Chunk) ON EACH [n.text, n.organisation_id]'\n"
    )
    assert "ORG005" not in _rules(src)


def test_org005_vector_index_with_interpolated_org_passes() -> None:
    src = (
        "ORG_PROPERTY = 'organisation_id'\n"
        "Q = f'CREATE VECTOR INDEX chunk_vec FOR (n:Chunk) "
        "ON (n.{ORG_PROPERTY}) OPTIONS {{}}'\n"
    )
    assert "ORG005" not in _rules(src)


# --- ORA-51: YAML as canonical source for ORG_SCOPED_LABELS (v2 mechanism) -----
#
# The v1 mirror constant in tools/lint/check_org_scoping.py is being replaced.
# The lint rule must read the YAML at lint time (no substrate import) so
# adding a label to the YAML is the *only* change needed for ORG003 to
# recognise it. The seam is a keyword-only `org_scoped_labels_yaml: Path | None`
# argument on `check_source`; when None, the rule resolves the canonical path.


def _write_yaml(path: Path, labels: list[str], relationship_types: list[str] | None = None) -> None:
    """Write a v1-schema YAML for the loader to consume."""
    rels = relationship_types or []
    text = "schema_version: 1\nlabels:\n"
    for label in labels:
        text += f'  - "{label}"\n'
    text += "relationship_types:\n"
    for rel in rels:
        text += f'  - "{rel}"\n'
    path.write_text(text, encoding="utf-8")


def test_org003_new_yaml_label_is_recognised_with_no_other_code_change(tmp_path: Path) -> None:
    """Brief AC, restated: adding a label to the YAML makes ORG003 flag a DDL
    omitting org on that label, with no other code change."""
    yaml_path = tmp_path / "org_scoped_labels.yaml"
    _write_yaml(yaml_path, ["__Entity__", "__Community__", "__Contradiction__", "Chunk", "FooBar"])
    src = "Q = 'CREATE INDEX foobar_idx IF NOT EXISTS FOR (n:`FooBar`) ON (n.graph_id)'\n"
    violations = check_source(src, org_scoped_labels_yaml=yaml_path)
    assert any(v.rule == "ORG003" for v in violations), violations


def test_org003_yaml_label_with_org_passes(tmp_path: Path) -> None:
    """Symmetry guard: the same new label, with org in the ON clause, does not
    flag — the recognition only fires when org is missing."""
    yaml_path = tmp_path / "org_scoped_labels.yaml"
    _write_yaml(yaml_path, ["__Entity__", "Chunk", "FooBar"])
    src = (
        "Q = 'CREATE INDEX foobar_idx IF NOT EXISTS "
        "FOR (n:`FooBar`) ON (n.organisation_id, n.graph_id)'\n"
    )
    violations = check_source(src, org_scoped_labels_yaml=yaml_path)
    assert not any(v.rule == "ORG003" for v in violations), violations


def test_org003_recognition_drops_label_when_yaml_drops_it(tmp_path: Path) -> None:
    """Negative-side symmetry: removing a label from the YAML removes ORG003
    coverage on it. Together with the additive test this proves the rule's
    recognition set IS the YAML, not a superset / subset."""
    yaml_path = tmp_path / "org_scoped_labels.yaml"
    _write_yaml(yaml_path, ["Chunk"])  # __Entity__ deliberately dropped
    src = "Q = 'CREATE INDEX entity_idx IF NOT EXISTS FOR (n:`__Entity__`) ON (n.graph_id)'\n"
    violations = check_source(src, org_scoped_labels_yaml=yaml_path)
    assert not any(v.rule == "ORG003" for v in violations), violations


def test_org003_existing_canonical_yaml_recognises_entity(tmp_path: Path) -> None:
    """The lint resolves to the canonical YAML when no override is passed and
    finds ``__Entity__`` (and the other current four) there — equivalent to
    the v1 behaviour, now sourced from the YAML."""
    # No override => the rule reads the canonical
    # packages/substrate/.../schema/org_scoped_labels.yaml.
    src = "Q = 'CREATE INDEX entity_idx IF NOT EXISTS FOR (n:`__Entity__`) ON (n.graph_id)'\n"
    assert "ORG003" in _rules(src)


def test_no_mirror_constant_in_lint_source() -> None:
    """The structural-drift-impossible property: the lint module must not
    declare an ``ORG_SCOPED_NEO4J_LABELS`` mirror constant. The YAML at
    ``packages/substrate/.../schema/org_scoped_labels.yaml`` is the single
    source of truth; a Python mirror re-opens the drift surface the v2
    swap exists to close."""
    import inspect

    import tools.lint.check_org_scoping as mod

    source = inspect.getsource(mod)
    assert "ORG_SCOPED_NEO4J_LABELS" not in source, (
        "lint module must not declare an ORG_SCOPED_NEO4J_LABELS mirror "
        "constant; the YAML at packages/substrate/src/oraclous_substrate/"
        "schema/org_scoped_labels.yaml is the single source of truth (ORA-51)"
    )


def test_check_source_accepts_org_scoped_labels_yaml_kwarg(tmp_path: Path) -> None:
    """The testability seam: ``check_source`` must accept the keyword-only
    ``org_scoped_labels_yaml`` path. Without this signature, the lint cannot
    be unit-tested for YAML-driven recognition (would have to mutate the
    canonical YAML at the canonical path — a global side effect)."""
    import inspect

    sig = inspect.signature(check_source)
    assert "org_scoped_labels_yaml" in sig.parameters, list(sig.parameters)
    param = sig.parameters["org_scoped_labels_yaml"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY, param.kind
