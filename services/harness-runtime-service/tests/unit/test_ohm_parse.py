"""OHM v1 thin loader (slice 1): valid load + the fail-closed error taxonomy. No DB / no network."""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.ohm.errors import (
    OHMParseError,
    OHMSchemaError,
    OHMVersionError,
)
from oraclous_harness_runtime_service.domain.ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_MINIMAL = """
ohm_version: "1.0"
metadata:
  id: "01976e3a-7c9b-7b00-9c45-1234567890ab"
  name: "Hello Harness"
  owner_organization_id: "01976e3a-0000-7000-9c45-000000000000"
capabilities:
  - ref: "core/echo@1.0.0"
    binding: "echo"
models:
  - role: "primary"
    binding: "anthropic/claude-opus-4-8"
    protocol_shape: "native"
prompts:
  - role: "primary"
    source: "inline"
    body: "You are a helpful assistant."
runtime:
  entrypoint: "echo"
"""


def test_loads_minimal_manifest() -> None:
    m = load_ohm(_MINIMAL)
    assert m.ohm_version == "1.0"
    assert m.metadata.name == "Hello Harness"
    assert m.entrypoint_capability() is not None
    assert m.entrypoint_capability().binding == "echo"
    assert m.primary_model().protocol_shape == "native"
    assert m.primary_prompt().body.startswith("You are")


def test_accepts_already_parsed_object() -> None:
    import yaml

    m = load_ohm(yaml.safe_load(_MINIMAL))
    assert m.metadata.name == "Hello Harness"


def test_malformed_yaml_raises_parse_error() -> None:
    with pytest.raises(OHMParseError):
        load_ohm("ohm_version: '1.0'\n  bad: : indent")


def test_unsupported_version_raises_version_error() -> None:
    with pytest.raises(OHMVersionError):
        load_ohm(_MINIMAL.replace('"1.0"', '"2.0"'))


def test_missing_required_field_raises_schema_error() -> None:
    bad = _MINIMAL.replace('  name: "Hello Harness"\n', "")
    with pytest.raises(OHMSchemaError):
        load_ohm(bad)


def test_entrypoint_must_match_a_capability_binding() -> None:
    bad = _MINIMAL.replace('entrypoint: "echo"', 'entrypoint: "nope"')
    with pytest.raises(OHMSchemaError):
        load_ohm(bad)


def test_duplicate_capability_binding_rejected() -> None:
    # two capabilities sharing a binding would silently shadow each other downstream (H2).
    dup = _MINIMAL.replace(
        '  - ref: "core/echo@1.0.0"\n    binding: "echo"\n',
        '  - ref: "core/echo@1.0.0"\n    binding: "echo"\n'
        '  - ref: "core/other@1.0.0"\n    binding: "echo"\n',
    )
    with pytest.raises(OHMSchemaError):
        load_ohm(dup)


def test_non_mapping_document_raises_parse_error() -> None:
    with pytest.raises(OHMParseError):
        load_ohm("- just\n- a\n- list")
