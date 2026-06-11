"""Unit test for the worker's read-after-write race handling (#267).

When the job row is not yet visible to the worker's fresh session (`load_payload` -> None), the
task must RAISE `JobNotVisibleYet` (so Celery retries with backoff) rather than silently returning a
`{"status": "missing"}` success that drops the submission. No real Postgres/Neo4j/broker: the
engine, sessionmaker, and Neo4j driver factories are patched, and the repo's `load_payload` is
stubbed to return None to simulate the row not being visible yet.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from oraclous_knowledge_graph_service.tasks import ingest_tasks
from oraclous_knowledge_graph_service.tasks.ingest_tasks import JobNotVisibleYet, _ingest_async

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-00000000050a")
_JOB = uuid.uuid4()


def _patch_worker_substrate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the worker engine/sessionmaker/driver factories so no real substrate is touched."""

    @asynccontextmanager
    async def _session_cm():
        yield MagicMock(name="session", commit=AsyncMock())

    fake_maker = MagicMock(side_effect=lambda: _session_cm())

    monkeypatch.setattr(ingest_tasks, "make_worker_engine", lambda: MagicMock(dispose=AsyncMock()))
    monkeypatch.setattr(ingest_tasks, "make_sessionmaker", lambda _engine: fake_maker)
    monkeypatch.setattr(
        ingest_tasks, "make_neo4j_driver", lambda _settings: MagicMock(close=MagicMock())
    )


async def test_worker_missing_row_raises_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 'missing' (not-yet-visible) path raises JobNotVisibleYet, not a silent success."""
    _patch_worker_substrate(monkeypatch)

    # The repo can't find the row in the worker's session -> simulate the race.
    repo = MagicMock(load_payload=AsyncMock(return_value=None))
    monkeypatch.setattr(ingest_tasks, "IngestionJobRepository", lambda _session: repo)

    with pytest.raises(JobNotVisibleYet):
        await _ingest_async(str(_JOB), str(_ORG))


def test_task_autoretries_on_job_not_visible() -> None:
    """The Celery task is wired to retry (bounded) on JobNotVisibleYet, not swallow it."""
    task = ingest_tasks.ingest_document_task
    assert JobNotVisibleYet in task.autoretry_for
    assert task.max_retries == 5
    assert task.retry_backoff is True
