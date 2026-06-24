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

_DEFAULT_TIMEOUT_S = 30.0


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

    async def execute(self, input_data: Any, context: ExecutionContext) -> ExecutionResult:
        started = time.monotonic()
        try:
            if not isinstance(input_data, dict):
                return ExecutionResult(
                    success=False,
                    error_message="input must be a JSON object",
                    error_type="INVALID_INPUT",
                )
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
                success=False, error_message=str(exc), error_type=type(exc).__name__
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
