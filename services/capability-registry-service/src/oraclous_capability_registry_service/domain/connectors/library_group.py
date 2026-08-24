"""Library-group connector (domain layer) — a curated library as a tool group (#488).

Mounts a curated in-repo library's exported functions as one tool with one operation per
function (ADR-038 D1). Dispatches IN-PROCESS by ``input_data['operation']`` to the matching
curated callable (from :mod:`domain.libraries.registry`), validates its typed args, and returns
the function's dict as ``output_data`` on the org-scoped Execution row. The harness already emits
one ToolSpec per operation (``binding__op``), so a member binds the group token and gets every
operation, each ceiling-checked — zero harness change.

Because a member binds a GROUP and gets everything in it, each curated library is a separate group
with its own executor subclass (#822): ``GROUP`` names the only operations that executor will
dispatch, so a math member is never handed an e-mail extractor and a text member is never handed a
compounding function. Separation holds at dispatch, not only in the advertised list.

CURATED + in-process: these are trusted, code-reviewed platform functions, so there is no
subprocess/RLIMIT isolation (that envelope, #487's, is for USER-supplied code — a follow-up). The
InternalTool base still wraps every call in a hard timeout + a uniform error map, so any unforeseen
function exception is structured, never a leaked traceback.
"""

from __future__ import annotations

import asyncio
from typing import Any

from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)
from oraclous_capability_registry_service.domain.libraries.registry import (
    MATH_TOOLS,
    TEXT_TOOLS,
    get_operation,
    operation_names,
)

#: hard cap on any string argument — bounds CPU/memory on a hostile input before dispatch.
_MAX_ARG_CHARS = 100_000


def _type_matches(value: Any, expected: type) -> bool:
    """Does ``value`` satisfy an operation's declared arg type?

    ``bool`` is never a number, however it is declared: it is an ``int`` subclass, so without the
    explicit rejection ``True`` would arrive at a curated function as 1.

    A whole number IS accepted where a decimal is declared. Every money argument is naturally a
    ``float`` and ``isinstance(40000, float)`` is ``False``, so without this a member sending a
    round figure — which is what a member sends — has its call rejected as INVALID_INPUT.

    The widening runs ONE WAY. A decimal is NOT accepted where a whole number is declared: a count
    of periods is a count, and ``1.08 ** 2.5`` is arithmetically valid, which is precisely the
    danger — a confident figure for a question nobody asked.
    """
    if isinstance(value, bool):
        return False
    if expected is float:
        return isinstance(value, int | float)
    return isinstance(value, expected)


class LibraryGroupExecutor(InternalTool):
    """Dispatches a curated library operation in-process and returns its dict output (#488)."""

    #: The curated group this executor serves. Subclasses override it; an operation outside the
    #: group is INVALID_OPERATION here, whatever the descriptor advertises.
    GROUP: str = TEXT_TOOLS

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        known = operation_names(self.GROUP)
        operation = input_data.get("operation")
        if not isinstance(operation, str) or operation not in known:
            return ExecutionResult(
                success=False,
                error_message=f"'operation' must be one of {known}",
                error_type="INVALID_OPERATION",
            )
        spec = get_operation(operation, self.GROUP)
        assert spec is not None  # noqa: S101 — membership just checked above
        kwargs: dict[str, Any] = {}
        for name, expected in spec.args.items():
            value = input_data.get(name)
            if not _type_matches(value, expected):
                return ExecutionResult(
                    success=False,
                    error_message=f"'{name}' must be a {expected.__name__}",
                    error_type="INVALID_INPUT",
                )
            # Cap string args so a hostile input can't drive a function into pathological cost.
            # The numeric equivalent lives in the curated function itself (a bounded period count),
            # because only the function knows which of its arguments drives the cost.
            if isinstance(value, str) and len(value) > _MAX_ARG_CHARS:
                return ExecutionResult(
                    success=False,
                    error_message=f"'{name}' exceeds the {_MAX_ARG_CHARS}-character limit",
                    error_type="INVALID_INPUT",
                )
            kwargs[name] = value
        # Trusted curated code, but run OFF the event loop (asyncio.to_thread) so one CPU-bound
        # cannot freeze other tenants on this worker; the InternalTool outer timeout + the uniform
        # exception→structured-result map still apply. (A runaway thread can't be killed, so the
        # primary bound is the curated functions staying linear + the arg-length cap above.)
        data = await asyncio.to_thread(spec.func, **kwargs)
        return ExecutionResult(success=True, data=data, metadata={"operation": operation})


class MathToolsExecutor(LibraryGroupExecutor):
    """The ``math-tools`` group (#822) — the same dispatch, a different set of operations."""

    GROUP = MATH_TOOLS
