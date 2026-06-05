"""Unit: InternalTool wrapper (validation, error mapping, timeout) + credential redaction."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)

pytestmark = pytest.mark.unit


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        credentials={"connection_string": {"connection_string": "secret-dsn"}},
    )


class _Echo(InternalTool):
    async def _execute_internal(self, input_data, context) -> ExecutionResult:
        return ExecutionResult(success=True, data={"echo": input_data})


class _Boom(InternalTool):
    async def _execute_internal(self, input_data, context) -> ExecutionResult:
        raise RuntimeError("kaboom")


class _Slow(InternalTool):
    timeout_s = 0.02

    async def _execute_internal(self, input_data, context) -> ExecutionResult:
        await asyncio.sleep(1)
        return ExecutionResult(success=True)


async def test_success_path_sets_credits_and_time() -> None:
    result = await _Echo({}).execute({"a": 1}, _ctx())
    assert result.success and result.data == {"echo": {"a": 1}}
    assert result.credits_consumed == 1
    assert result.processing_time_ms is not None


async def test_non_dict_input_is_rejected() -> None:
    result = await _Echo({}).execute("not-a-dict", _ctx())
    assert not result.success and result.error_type == "INVALID_INPUT"


async def test_exception_is_mapped_to_failure() -> None:
    result = await _Boom({}).execute({}, _ctx())
    assert not result.success and result.error_type == "RuntimeError"


async def test_timeout_is_enforced() -> None:
    result = await _Slow({}).execute({}, _ctx())
    assert not result.success and result.error_type == "TIMEOUT"


def test_context_repr_redacts_credentials() -> None:
    ctx = _ctx()
    assert "secret-dsn" not in repr(ctx)
    assert "redacted" in repr(ctx)
    ctx.scrub()
    assert ctx.credentials == {}
