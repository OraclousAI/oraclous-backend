"""Tool executor base classes (domain layer; reshape of legacy
``oraclous-core-service/app/tools/base/*`` + ``interfaces/tool_executor``).

``ExecutionContext`` carries the resolved credentials into a single execution; its ``__repr__``
redacts them so secrets never leak into logs/tracebacks. The legacy ``workflow_id``/``job_id`` are
dropped (ADR-005). ``BaseToolExecutor`` is the contract; ``InternalTool`` wraps the concrete
``_execute_internal`` with validation, a hard timeout, credit accounting and uniform error mapping;
``DatabaseTool`` adds connection-string resolution.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from oraclous_capability_registry_service.domain.executors.input_validation import validate_input

_DEFAULT_TIMEOUT_S = 30.0

#: Exception types whose message is derived from the caller's own data rather than written for a
#: caller: ``str(KeyError("path"))`` is ``"'path'"``. Surfacing one names no argument and no
#: expectation, so the caller cannot repair the call — and a KeyError's message is caller-supplied
#: content that has no business being echoed back (#693). Their message is replaced; the exception
#: type is still reported, so an operator reading a trace loses nothing.
_UNCURATED_MESSAGE = (KeyError, IndexError, AttributeError, TypeError)


class ExecutionContext(BaseModel):
    instance_id: uuid.UUID
    organisation_id: uuid.UUID
    user_id: uuid.UUID
    execution_id: uuid.UUID
    # The verified principal type that invoked the tool (the value of the gateway's
    # X-Principal-Type, e.g. ``user`` / ``agent``). First-party connectors forward it downstream
    # (ADR-018) so the called service scopes to the SAME principal kind — not a hardcoded type.
    # Defaults to ``agent`` (the harness loop is the dominant caller) for paths that don't set it.
    principal_type: str = "agent"
    # credential_type -> resolved payload (e.g. a connection_string or an access_token)
    credentials: dict[str, Any] = Field(default_factory=dict)
    configuration: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    # File-native blackboard (ADR-040 / #512): the team's real git-markdown working tree the file
    # tools read/write IN PLACE. None → the default per-org scratch sandbox (legacy behaviour).
    working_dir: str | None = None

    def __repr__(self) -> str:  # redact secrets from logs/tracebacks
        keys = sorted(self.credentials)
        return (
            f"ExecutionContext(instance_id={self.instance_id}, execution_id={self.execution_id}, "
            f"credentials=<redacted: {keys}>)"
        )

    __str__ = __repr__

    def scrub(self) -> None:
        """Drop the in-memory credential material once execution is done."""
        self.credentials = {}


class ExecutionResult(BaseModel):
    success: bool
    data: Any | None = None
    error_message: str | None = None
    error_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    credits_consumed: Decimal = Decimal("0")
    processing_time_ms: int | None = None


def _safe_message(exc: Exception) -> str:
    """The error detail for an unexpected tool failure — never a raw uncurated exception message.

    #693 AC2: the caller of a tool is a model, and it acts on this string. ``'path'`` (a KeyError)
    and ``list index out of range`` (an IndexError) tell it nothing it can fix, and the first is
    caller-supplied data reflected back. Everything else keeps its message: our own exceptions are
    written for a caller.
    """
    if isinstance(exc, _UNCURATED_MESSAGE):
        return (
            f"the tool failed while reading its arguments ({type(exc).__name__}) — check the call "
            "against the tool's declared input schema"
        )
    return str(exc)


class BaseToolExecutor(ABC):
    """Contract: execute a tool with input + a resolved execution context."""

    def __init__(self, descriptor: dict[str, Any]) -> None:
        self.descriptor = descriptor

    @abstractmethod
    async def execute(self, input_data: Any, context: ExecutionContext) -> ExecutionResult: ...

    def calculate_credits(self, input_data: Any, result: ExecutionResult) -> Decimal:
        return Decimal("1")


class InternalTool(BaseToolExecutor):
    """Base for in-process tools: validation + hard timeout + credits + uniform error mapping."""

    timeout_s: float = _DEFAULT_TIMEOUT_S

    def _schema_problem(self, input_data: dict[str, Any], context: ExecutionContext) -> str | None:
        """The call's first violation of the descriptor's declared ``input_schema``, else None.

        #693: the schema was declared and never enforced, so a connector subscripting a declared
        key turned a malformed call into a raw ``KeyError``. Enforcing it HERE binds the check to
        every builtin connector — they are all ``InternalTool`` subclasses — rather than to the one
        connector the bug was reported on. A descriptor with no schema (the unit-test construction
        ``Connector({"id": "x"})``) validates nothing, exactly as before.
        """
        schema = (self.descriptor.get("spec") or {}).get("input_schema")
        if not isinstance(schema, dict):
            return None
        return validate_input(schema, input_data, configuration=context.configuration)

    async def execute(self, input_data: Any, context: ExecutionContext) -> ExecutionResult:
        started = time.monotonic()
        try:
            if not isinstance(input_data, dict):
                return ExecutionResult(
                    success=False,
                    error_message="input must be a JSON object",
                    error_type="INVALID_INPUT",
                )
            problem = self._schema_problem(input_data, context)
            if problem is not None:
                # Rejected BEFORE _execute_internal, so a malformed call never reaches a network
                # call, a credential or a side effect.
                result = ExecutionResult(
                    success=False, error_message=problem, error_type="INVALID_INPUT"
                )
            else:
                result = await asyncio.wait_for(
                    self._execute_internal(input_data, context), timeout=self.timeout_s
                )
                result.credits_consumed = self.calculate_credits(input_data, result)
        except TimeoutError:
            result = ExecutionResult(
                success=False,
                error_message=f"execution exceeded {self.timeout_s}s timeout",
                error_type="TIMEOUT",
            )
        except Exception as exc:  # noqa: BLE001 — map any tool failure to a structured result
            result = ExecutionResult(
                success=False, error_message=_safe_message(exc), error_type=type(exc).__name__
            )
        result.processing_time_ms = int((time.monotonic() - started) * 1000)
        return result

    @abstractmethod
    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult: ...

    @staticmethod
    def get_credentials(context: ExecutionContext, cred_type: str) -> dict[str, Any] | None:
        return context.credentials.get(cred_type)


class DatabaseTool(InternalTool):
    """Base for relational-DB connectors: resolves the connection string from the context."""

    def get_connection_string(self, context: ExecutionContext) -> str:
        creds = self.get_credentials(context, "connection_string")
        if not creds or not creds.get("connection_string"):
            raise ValueError("connection_string credential not found in execution context")
        return str(creds["connection_string"])
