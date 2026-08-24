"""#828 item 2 — a step's timestamps must actually round-trip through real Postgres.

The unit suite (``test_step_timestamps.py``) proves ``StepOut``/``LoopStep``/``_serialize_steps``
carry the pair; none of it proves the JSONB write itself succeeds. A raw ``datetime`` in the
serialized dict crashes the write — SQLAlchemy's JSON column has no ``datetime`` serializer — which
unit tests never catch (they never touch a real column). Caught live on PR #859's deployed-stack
e2e: every ``POST /v1/harnesses/execute`` that reached a real tool call 500'd with
``TypeError: Object of type datetime is not JSON serializable``.

Key-free: a testcontainer Postgres; the repo self-binds the org (ADR-030 org_scope).
"""

from __future__ import annotations

import datetime as _dt
import uuid
from collections.abc import AsyncIterator

import pytest

pytestmark = [pytest.mark.integration]

_ORG = "00000000-0000-0000-0000-00000000060b"
_T0 = _dt.datetime(2026, 8, 21, 9, 0, 0, tzinfo=_dt.UTC)
_T1 = _dt.datetime(2026, 8, 21, 9, 0, 12, tzinfo=_dt.UTC)


@pytest.fixture
async def repo(postgres_dsn: str) -> AsyncIterator[object]:
    async_dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    from oraclous_harness_runtime_service.models import Base
    from sqlalchemy.ext.asyncio import create_async_engine

    setup = create_async_engine(async_dsn)
    async with setup.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await setup.dispose()

    from oraclous_harness_runtime_service.repositories.execution_repository import (
        ExecutionRepository,
    )

    r = ExecutionRepository(async_dsn)
    yield r
    await r.close()


async def test_a_steps_timestamps_survive_a_real_write_and_read(repo: object) -> None:
    from oraclous_harness_runtime_service.domain.loop.tool_use import LoopStep
    from oraclous_harness_runtime_service.models.enums import StepKind
    from oraclous_harness_runtime_service.services.harness_execution_service import (
        _serialize_steps,
    )

    steps = _serialize_steps(
        [
            LoopStep(
                index=0,
                kind=StepKind.TOOL,
                name="graph.search",
                status="ok",
                started_at=_T0,
                ended_at=_T1,
            )
        ]
    )
    execution_id = uuid.uuid4()

    row = await repo.create(  # type: ignore[attr-defined]
        execution_id=execution_id,
        organisation_id=uuid.UUID(_ORG),
        user_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        harness_name="T",
        content_hash=None,
        status="SUCCEEDED",
        input_text="go",
        output="ok",
        error_type=None,
        error_message=None,
        iterations=1,
        total_tokens=1,
        steps=steps,
        trace_id=None,
        parent_execution_id=None,
    )

    assert row.steps[0]["started_at"] == _T0.isoformat()
    assert row.steps[0]["ended_at"] == _T1.isoformat()

    fetched = await repo.get(execution_id, uuid.UUID(_ORG))  # type: ignore[attr-defined]
    assert fetched is not None
    assert fetched.steps[0]["started_at"] == _T0.isoformat()
    assert fetched.steps[0]["ended_at"] == _T1.isoformat()
