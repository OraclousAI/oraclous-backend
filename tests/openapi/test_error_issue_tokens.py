"""Unit: the openapi contract pins ``ErrorDetail``'s stable-token allowlist and documents the new
``MODEL_CREDENTIAL_REJECTED`` code + the ``INVALID_GRAPH_ID`` detail token on their endpoints
(#1108 rulings 6-7); #1151 adds ``MODEL_ANSWER_UNUSABLE`` alongside it — the model answered, but
the read-back could not use what came back (every unparseable read, every non-SUCCEEDED terminal
other than a credential rejection).

``x-stable-issue-tokens`` is the LEAST-COMMITMENT list: only ``INVALID_GRAPH_ID`` is promised not
to change shape; every other ``details[].issue`` value (READBACK_FAILED, READER_OUTPUT_UNPARSEABLE,
and any Pydantic-derived token such as STRING_TOO_SHORT or MISSING) stays free to change. This
pins STRUCTURE — the allowlist contents and the presence of the two documented codes/tokens on
their operations — not prose, except the one "may change" caveat on the ``issue`` property, which
is asserted only as a loose, case-insensitive substring.

RED until the [impl] PR (I6) adds ``x-stable-issue-tokens``, widens the ``issue`` description, adds
``MODEL_CREDENTIAL_REJECTED`` to the error-code enum, and documents both on their responses —
``openapi/v1.yaml`` is untouched by this [tests] PR.

#1151 (I4) additionally: the error-code enum gains ``MODEL_ANSWER_UNUSABLE``; the read-back
operation documents a ``502`` response carrying it (a new response, not yet present); and the
read-back ``422`` description no longer claims reader/parse failures — those terminal states move
to 502, so 422 is left to describe only the guards that genuinely never call the model.
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


def test_error_code_enum_lists_model_answer_unusable() -> None:
    assert "MODEL_ANSWER_UNUSABLE" in _error_code_enum(_load_spec())


def test_readback_502_documents_model_answer_unusable() -> None:
    spec = _load_spec()
    responses = spec["paths"]["/v1/engine/intake/readback"]["post"]["responses"]
    assert "502" in responses, responses
    resolved = _resolve(spec, responses["502"])
    assert "MODEL_ANSWER_UNUSABLE" in resolved["description"], resolved


def test_readback_422_no_longer_claims_reader_failures() -> None:
    """The unparseable-read and non-SUCCEEDED-terminal cases move to 502 (#1151); 422 is left to
    describe only the guards that never call the model (IDEA_TOO_VAGUE) and the credential refusal
    (MODEL_CREDENTIAL_REJECTED), never an unparseable or failed reader run."""
    spec = _load_spec()
    response = spec["paths"]["/v1/engine/intake/readback"]["post"]["responses"]["422"]
    resolved = _resolve(spec, response)
    description = resolved["description"].lower()
    for forbidden in ("unparseable", "failed reader", "reader run"):
        assert forbidden not in description, resolved
