"""Unit: the openapi contract pins ``ErrorDetail``'s stable-token allowlist and documents the new
``MODEL_CREDENTIAL_REJECTED`` code + the ``INVALID_GRAPH_ID`` detail token on their endpoints
(#1108 rulings 6-7).

``x-stable-issue-tokens`` is the LEAST-COMMITMENT list: only ``INVALID_GRAPH_ID`` is promised not
to change shape; every other ``details[].issue`` value (READBACK_FAILED, READER_OUTPUT_UNPARSEABLE,
and any Pydantic-derived token such as STRING_TOO_SHORT or MISSING) stays free to change. This
pins STRUCTURE — the allowlist contents and the presence of the two documented codes/tokens on
their operations — not prose, except the one "may change" caveat on the ``issue`` property, which
is asserted only as a loose, case-insensitive substring.

RED until the [impl] PR (I6) adds ``x-stable-issue-tokens``, widens the ``issue`` description, adds
``MODEL_CREDENTIAL_REJECTED`` to the error-code enum, and documents both on their responses —
``openapi/v1.yaml`` is untouched by this [tests] PR.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_SPEC_PATH = Path(__file__).resolve().parents[2] / "openapi" / "v1.yaml"


def _load_spec() -> dict[str, Any]:
    return yaml.safe_load(_SPEC_PATH.read_text(encoding="utf-8"))


def _resolve(spec: dict[str, Any], node: Any) -> Any:
    """Resolve a single ``$ref`` hop against the spec root (the only indirection this spec uses,
    per ``validation_passthrough``'s own docstring and ``test_pagination_contract``'s precedent)."""
    if isinstance(node, dict) and "$ref" in node:
        cur: Any = spec
        for key in node["$ref"].lstrip("#/").split("/"):
            cur = cur[key]
        return cur
    return node


def _error_detail_schema(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["components"]["schemas"]["ErrorDetail"]


def _error_code_enum(spec: dict[str, Any]) -> list[str]:
    envelope = spec["components"]["schemas"]["ErrorEnvelope"]
    return envelope["properties"]["error"]["properties"]["code"]["enum"]


def test_error_detail_declares_exactly_the_stable_issue_tokens_allowlist() -> None:
    schema = _error_detail_schema(_load_spec())
    assert schema.get("x-stable-issue-tokens") == ["INVALID_GRAPH_ID"], schema


def test_error_detail_issue_description_warns_unlisted_tokens_may_change() -> None:
    schema = _error_detail_schema(_load_spec())
    description = schema["properties"]["issue"]["description"]
    assert "may change" in description.lower(), description


def test_error_code_enum_lists_model_credential_rejected() -> None:
    assert "MODEL_CREDENTIAL_REJECTED" in _error_code_enum(_load_spec())


def test_readback_422_documents_model_credential_rejected() -> None:
    spec = _load_spec()
    response = spec["paths"]["/v1/engine/intake/readback"]["post"]["responses"]["422"]
    resolved = _resolve(spec, response)
    assert "MODEL_CREDENTIAL_REJECTED" in resolved["description"], resolved


def test_apps_runs_422_documents_invalid_graph_id() -> None:
    spec = _load_spec()
    response = spec["paths"]["/v1/engine/apps/{app_id}/runs"]["post"]["responses"]["422"]
    resolved = _resolve(spec, response)
    assert "INVALID_GRAPH_ID" in resolved["description"], resolved
