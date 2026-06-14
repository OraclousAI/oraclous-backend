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


def _structured_maker():
    @asynccontextmanager
    async def _session_cm():
        yield MagicMock(name="session")

    return MagicMock(side_effect=lambda: _session_cm())


class _Sentinel(Exception):
    """Raised from get_ontology to prove execution passed the draft guard."""


async def test_structured_ingest_rejects_a_draft_recipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The async structured-ingest path halts on a DRAFT recipe BEFORE any graph write (ADR-028)."""
    repo = MagicMock(get_latest=AsyncMock(return_value={"id": "rcp_x", "status": "draft"}))
    monkeypatch.setattr(ingest_tasks, "RecipeRepository", lambda _session: repo)
    payload = MagicMock(recipe_id="rcp_x")
    with pytest.raises(RuntimeError, match="draft; promote it before ingesting"):
        await ingest_tasks._ingest_structured(
            driver=MagicMock(),
            maker=_structured_maker(),
            settings=MagicMock(),
            payload=payload,
            data=b"name,age\na,1\n",
        )


async def test_structured_ingest_lets_a_promoted_recipe_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PROMOTED recipe passes the guard and reaches ontology load (proven via a sentinel)."""
    repo = MagicMock(get_latest=AsyncMock(return_value={"id": "rcp_x", "status": "promoted"}))
    monkeypatch.setattr(ingest_tasks, "RecipeRepository", lambda _session: repo)
    graph_repo = MagicMock(get_ontology=AsyncMock(side_effect=_Sentinel()))
    monkeypatch.setattr(ingest_tasks, "GraphRepository", lambda _session: graph_repo)
    payload = MagicMock(recipe_id="rcp_x")
    # Reaching get_ontology (the step after the guard) proves the promoted recipe was not blocked.
    with pytest.raises(_Sentinel):
        await ingest_tasks._ingest_structured(
            driver=MagicMock(),
            maker=_structured_maker(),
            settings=MagicMock(),
            payload=payload,
            data=b"name,age\na,1\n",
        )
