"""Library-group connector (ORAA-4 §21 domain layer) — a curated library as a tool group (#488).

Mounts a curated in-repo library's exported functions as one tool with one operation per
function (ADR-038 D1). Dispatches IN-PROCESS by ``input_data['operation']`` to the matching
curated callable (from :mod:`domain.libraries.registry`), validates its typed args, and returns
the function's dict as ``output_data`` on the org-scoped Execution row. The harness already emits
one ToolSpec per operation (``binding__op``), so a member binds the group token and gets every
operation, each ceiling-checked — zero harness change.

CURATED + in-process: these are trusted, code-reviewed platform functions, so there is no
subprocess/RLIMIT isolation (that envelope, #487's, is for USER-supplied code — a follow-up). The
InternalTool base still wraps every call in a hard timeout + a uniform error map, so any unforeseen
function exception is structured, never a leaked traceback.
"""

from __future__ import annotations

from typing import Any

from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)
from oraclous_capability_registry_service.domain.libraries.registry import (
    get_operation,
    operation_names,
)


class LibraryGroupExecutor(InternalTool):
    """Dispatches a curated library operation in-process and returns its dict output (#488)."""

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        operation = input_data.get("operation")
        if not isinstance(operation, str) or operation not in operation_names():
            return ExecutionResult(
                success=False,
                error_message=f"'operation' must be one of {operation_names()}",
                error_type="INVALID_OPERATION",
            )
        spec = get_operation(operation)
        assert spec is not None  # noqa: S101 — membership just checked above
        kwargs: dict[str, Any] = {}
        for name, expected in spec.args.items():
            value = input_data.get(name)
            # bool is an int subclass; none of the curated arg types are bool, so a plain
            # isinstance is correct here (reject a bool slipping in where a str is expected).
            if not isinstance(value, expected) or isinstance(value, bool):
                return ExecutionResult(
                    success=False,
                    error_message=f"'{name}' must be a {expected.__name__}",
                    error_type="INVALID_INPUT",
                )
            kwargs[name] = value
        # Trusted curated code, called in-process. The InternalTool base wraps this in the outer
        # timeout + the uniform exception→structured-result map, so a function bug never leaks.
        data = spec.func(**kwargs)
        return ExecutionResult(success=True, data=data, metadata={"operation": operation})
