"""Operations a tool descriptor declares, and the refusal for one it does not (domain layer).

One reading of ``spec.capabilities``, the list every descriptor uses to say which operations it
offers — the same list the harness runtime reads to build a member's tool menu
(``domain/tool_schemas.tool_specs_for``). The registry now reads it too, so the two services agree
about which operation an instance may run WITHOUT either trusting the other (#1004 item 1, the
defence-in-depth half of #956).

**The rule.** A caller that CHOOSES an operation must choose one the descriptor declares:

* no ``operation`` key at all → nothing was chosen, so the connector's own hardcoded default
  stands (``postgresql`` defaults to ``query``, ``github`` to ``list_files``). That default is
  connector code, not caller input, and is therefore not the #956 threat surface; several shipped
  callers — ``manifest-validate``, ``manifest-refine``, ``script-ingestion``, the team-run spec —
  legitimately call ``/execute`` with no ``operation``.
* an ``operation`` that the descriptor does not declare → refused, fail-closed (CLAUDE.md §3.5),
  including when the descriptor declares none at all: an operation nothing declares is an
  operation nothing authorises.

**The imported (MCP) path is validated, not exempt.** An MCP import is not dynamic at execute
time: ``McpImportService.import_server`` records exactly one operation per discovered tool —
``spec.capabilities = [{"name": <the server's tool name>, ...}]`` — and an org admin approves that
descriptor (``pending_approval`` → ``active``). So an imported instance's declared set is the
single tool the admin actually approved, and this check is a real gate the mcp connector never
had (it dispatches on ``spec.tool_name`` and strips ``operation`` outright — #698 D3).
"""

from __future__ import annotations

from typing import Any

from oraclous_capability_registry_service.domain.executors.base import TOOL_ERROR_CHARS

#: The field the registry dispatches on — the same key the harness binds at dispatch.
OPERATION_KEY = "operation"

#: The coded refusal, in this endpoint's own closed lowercase vocabulary (``pending_approval`` /
#: ``no_executor`` are its siblings). The harness's ``registry_client`` accepts exactly this token
#: shape across the leak boundary and owns the words that explain it to a member (#692).
UNSUPPORTED_OPERATION = "unsupported_operation"

_REFUSAL_PREFIX = "this tool does not declare the operation "
_REFUSAL_SUFFIX = " — call one of the operations it declares"


def declared_operations(descriptor: dict[str, Any]) -> frozenset[str]:
    """The operation names ``spec.capabilities`` declares.

    ``spec`` is JSONB and can hold anything a writer put there, so a non-list, a non-dict entry and
    an entry without a usable ``name`` all declare nothing. A ragged row must never widen the set
    into a wildcard — that would turn a malformed descriptor into an open door.
    """
    capabilities = (descriptor.get("spec") or {}).get("capabilities")
    if not isinstance(capabilities, list):
        return frozenset()
    return frozenset(
        op["name"]
        for op in capabilities
        if isinstance(op, dict) and isinstance(op.get("name"), str) and op["name"]
    )


def operation_is_declared(descriptor: dict[str, Any], input_data: Any) -> bool:  # noqa: ANN401
    """Whether this call may proceed as far as the executor.

    True when the call chose no operation (the connector's default stands) or chose one the
    descriptor declares. A non-string value is never declared — a type the key cannot legitimately
    hold is not a loophole into "absent".
    """
    if not isinstance(input_data, dict) or OPERATION_KEY not in input_data:
        return True
    requested = input_data[OPERATION_KEY]
    return isinstance(requested, str) and requested in declared_operations(descriptor)


def unsupported_operation_message(requested: object) -> str:
    """The refusal text, bounded by ``TOOL_ERROR_CHARS`` INCLUDING the echoed name.

    The name is echoed so the caller can see WHICH one was wrong (#692: an unactionable error gets
    repeated), but the whole message is fed back to a model and persisted into a run transcript, so
    the budget covers the sentence, not merely the value inside it — otherwise the wrapper's length
    is a free extension of the echo.
    """
    room = TOOL_ERROR_CHARS - len(_REFUSAL_PREFIX) - len(_REFUSAL_SUFFIX) - 2  # the two quotes
    return f"{_REFUSAL_PREFIX}'{str(requested)[:room]}'{_REFUSAL_SUFFIX}"
