"""Unit (WP-10 recurrence guardrail): every GATEWAY-OWNED list/collection operation in the
published gateway contract declares the OPTIONAL ``limit`` + ``offset`` pagination params.

This is the mechanism that keeps a NEW unbounded gateway collection endpoint from shipping: a GET
whose ``operationId`` begins with ``list`` (the project's naming convention for a collection read)
and that the GATEWAY itself implements must declare both pagination params — directly or via a
``$ref`` to the shared ``components/parameters/{LimitParam,OffsetParam}``. The companion
``openapi-diff-gate`` keeps the published contract additive; this test keeps it bounded.

SCOPE (WP-10): only the gateway's OWN collection endpoints (the chat/agents/keys/webhooks planes
under ``/v1/...``). The spec also publishes UPSTREAM-PROXIED collections (``/api/v1/...`` and the
``/v1/harnesses``/``/v1/engine`` planes — tags knowledge-graph/capabilities/harness/engine) that
the gateway forwards verbatim to another service; bounding those is each upstream service's own
job, not a gateway edge change, so they are listed in ``_PROXIED_OUT_OF_SCOPE`` and excluded. A new
GATEWAY-owned collection is still caught — it would not be on that allow-list.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_SPEC_PATH = Path(__file__).resolve().parents[2] / "openapi" / "v1.yaml"
_REQUIRED_PARAMS = {"limit", "offset"}

# Upstream-service collections surfaced through the gateway proxy (NOT gateway-implemented). Out of
# WP-10 scope: each is bounded by its owning service, not at the gateway edge. Identified by
# operationId so adding a new GATEWAY collection (a different id) is never silently excluded.
_PROXIED_OUT_OF_SCOPE = {
    "listGraphs",
    "listPendingCrossGraphCandidates",
    "listRecipes",
    "listTools",
    "listCapabilities",
    "listInstances",
    "listAgentBindings",
    "listExecutions",
    "listJobs",
    "listTasks",
    "listDocuments",  # #579: a KGS-proxied ingest-job list — bounding is KGS's job, not the edge
}


def _load_spec() -> dict:
    return yaml.safe_load(_SPEC_PATH.read_text(encoding="utf-8"))


def _param_name(spec: dict, param: dict) -> str | None:
    """The query-param name a parameter declares, resolving a ``$ref`` into
    ``components/parameters`` one hop (the only indirection the gateway spec uses)."""
    ref = param.get("$ref")
    if ref is not None:
        # "#/components/parameters/LimitParam" -> the referenced object
        node: object = spec
        for key in ref.lstrip("#/").split("/"):
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        param = node if isinstance(node, dict) else {}
    if param.get("in") != "query":
        return None
    name = param.get("name")
    return name if isinstance(name, str) else None


def _list_operations(spec: dict) -> list[tuple[str, str, dict]]:
    """(method, path, operation) for every GATEWAY-OWNED GET whose operationId names a collection
    read (``list*``) — the project's convention for an unbounded list/collection endpoint.
    Upstream-proxied collections (``_PROXIED_OUT_OF_SCOPE``) are excluded (see module docstring)."""
    out: list[tuple[str, str, dict]] = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        op = item.get("get")
        if not isinstance(op, dict):
            continue
        op_id = str(op.get("operationId", ""))
        if op_id.startswith("list") and op_id not in _PROXIED_OUT_OF_SCOPE:
            out.append(("get", path, op))
    return out


def test_there_are_list_operations_to_check() -> None:
    # guard against a vacuous pass: the heuristic must actually match the known collections.
    spec = _load_spec()
    op_ids = {op.get("operationId") for _, _, op in _list_operations(spec)}
    assert {
        "listChatThreads",
        "listChatMessages",
        "listPublishedAgents",
        "listIntegrationKeys",
        "listWebhookSubscriptions",
    } <= op_ids, op_ids


def test_every_list_operation_declares_pagination_params() -> None:
    spec = _load_spec()
    offenders: list[str] = []
    for method, path, op in _list_operations(spec):
        declared = {
            _param_name(spec, p) for p in (op.get("parameters") or []) if isinstance(p, dict)
        }
        if not _REQUIRED_PARAMS <= declared:
            missing = _REQUIRED_PARAMS - declared
            offenders.append(
                f"{method.upper()} {path} ({op.get('operationId')}) is missing {sorted(missing)}"
            )
    assert not offenders, "unbounded collection endpoint(s) without pagination:\n" + "\n".join(
        offenders
    )
