"""Unit: the openapi contract pins the member-error-codes surface (#1109 ruling A.1-4).

``getTeamRun`` carries ``x-stable-member-error-codes: [llm_credential_rejected]`` — the ONLY
per-role token promised not to change shape, mirroring ``ErrorDetail``'s
``x-stable-issue-tokens`` precedent (#1108, ``test_error_issue_tokens.py``). Its 200 response
schema declares ``member_error_codes`` as an object of string values, always present (never
missing), keyed by member role.

The refine-nl operation's 422 documents that a FAILED op-drafter run surfaces
MODEL_CREDENTIAL_REJECTED, mirroring the readback 422's existing pattern (#1108 ruling 7,
``openapi/v1.yaml:2732``). The shared ``/v1/search`` operation's 422 documents BOTH
MODEL_CREDENTIAL_REJECTED (embedding-provider key refused) and MODEL_CREDENTIAL_REQUIRED
(quota/rate-limit exhaustion) for its semantic and hybrid retrieval modes — federated search's
ADR-026 degrade-on-embed-failure behaviour is deliberately NOT pinned here.

This pins STRUCTURE (the stability marker + schema property + which responses document which
codes), not free prose, except the two 422 description checks which are loose, case-sensitive
substring checks matching the existing ``test_readback_422_documents_model_credential_rejected``
pattern.

RED until the [impl] PR (I6) adds ``x-stable-member-error-codes`` to ``getTeamRun``, gives its
200 response a schema with a ``member_error_codes`` object property, and documents
MODEL_CREDENTIAL_REJECTED / MODEL_CREDENTIAL_REQUIRED on the refine-nl and search 422 responses —
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
    per ``validation_passthrough``'s own docstring and ``test_pagination_contract``'s
    precedent)."""
    if isinstance(node, dict) and "$ref" in node:
        cur: Any = spec
        for key in node["$ref"].lstrip("#/").split("/"):
            cur = cur[key]
        return cur
    return node


def _get_team_run_op(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["paths"]["/v1/engine/team-runs/{teamRunId}"]["get"]


def test_get_team_run_declares_exactly_the_stable_member_error_codes_allowlist() -> None:
    op = _get_team_run_op(_load_spec())
    assert op.get("x-stable-member-error-codes") == ["llm_credential_rejected"], op


def test_get_team_run_200_schema_declares_member_error_codes_as_a_string_map() -> None:
    spec = _load_spec()
    op = _get_team_run_op(spec)
    response = _resolve(spec, op["responses"]["200"])
    schema = _resolve(spec, response["content"]["application/json"]["schema"])
    prop = schema["properties"]["member_error_codes"]
    assert prop["type"] == "object", prop
    assert prop["additionalProperties"] == {"type": "string"}, prop


def test_refine_nl_422_documents_model_credential_rejected() -> None:
    spec = _load_spec()
    response = spec["paths"]["/v1/engine/team-drafts/{teamDraftId}/refine-nl"]["post"]["responses"][
        "422"
    ]
    resolved = _resolve(spec, response)
    assert "MODEL_CREDENTIAL_REJECTED" in resolved["description"], resolved


def test_search_422_documents_both_model_credential_codes() -> None:
    spec = _load_spec()
    response = spec["paths"]["/v1/search"]["post"]["responses"]["422"]
    resolved = _resolve(spec, response)
    description = resolved["description"]
    assert "MODEL_CREDENTIAL_REJECTED" in description, resolved
    assert "MODEL_CREDENTIAL_REQUIRED" in description, resolved
