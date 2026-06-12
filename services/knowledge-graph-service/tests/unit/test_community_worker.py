"""Worker-task wiring smoke for community detection (#303) — the async path, no broker, no Neo4j.

The async detect path was untested. This exercises ``_detect_async`` (the body of the Celery task)
end-to-end with the engine / Neo4j driver / Redis factories monkeypatched to fakes, asserting the
job-row lifecycle (running → completed), that the request params on ``source_content`` are decoded
and applied (min_entities/force_rebuild), that the per-graph lock client is passed to the repo, and
that the detect actually fired. No network, no DB, no real Celery.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from oraclous_knowledge_graph_service.services.analytics_service import encode_detect_params
from oraclous_knowledge_graph_service.tasks import community_tasks

pytestmark = pytest.mark.unit

_ORG = "11111111-1111-1111-1111-111111111111"
_GRAPH = uuid.uuid4()


class _FakeJobRepo:
    """Captures the status transitions the worker writes; serves the seeded payload."""

    transitions: list[dict] = []

    def __init__(self, session) -> None:  # noqa: ANN001, ARG002
        pass

    async def load_payload(self, job_id):  # noqa: ANN001, ARG002
        from oraclous_knowledge_graph_service.domain.job import IngestionPayload

        return IngestionPayload(
            graph_id=_GRAPH,
            source_type="community_detect",
            filename=None,
            source_content=encode_detect_params(min_entities=4, force_rebuild=True),
            recipe_id=None,
            valid_from=None,
            valid_to=None,
            event_time=None,
        )

    async def update_status(self, job_id, **kwargs):  # noqa: ANN001
        _FakeJobRepo.transitions.append(kwargs)


class _FakeRepo:
    """Records the lock client it was built with + the detect call; returns a 2-level dendrogram."""

    built_with_lock = None
    detect_called = False
    seen_min_entities = None

    def __init__(self, driver, *, database=None, lock_client=None) -> None:  # noqa: ANN001, ARG002
        _FakeRepo.built_with_lock = lock_client

    def count_entities(self, *, graph_id):  # noqa: ANN001, ARG002
        return 50

    def status(self, *, graph_id):  # noqa: ANN001, ARG002
        return 0, [], 50  # no existing communities

    def detect(self, *, graph_id):  # noqa: ANN001, ARG002
        _FakeRepo.detect_called = True
        return {0: {"L0:1": ["e1", "e2", "e3"]}, 1: {"L1:1": ["e1", "e2"], "L1:3": ["e3"]}}


@pytest.fixture(autouse=True)
def _reset() -> None:
    _FakeJobRepo.transitions = []
    _FakeRepo.built_with_lock = None
    _FakeRepo.detect_called = False


async def test_detect_async_wires_job_lifecycle_and_applies_params(monkeypatch) -> None:
    sentinel_lock = object()

    class _FakeSession:
        async def commit(self) -> None:
            return None

    @asynccontextmanager
    async def _fake_session():
        yield _FakeSession()

    class _FakeMaker:
        def __call__(self):
            return _fake_session()

    monkeypatch.setattr(community_tasks, "make_worker_engine", lambda: _FakeEngine())
    monkeypatch.setattr(community_tasks, "make_sessionmaker", lambda _engine: _FakeMaker())
    monkeypatch.setattr(community_tasks, "make_neo4j_driver", lambda _s: _FakeDriver())
    monkeypatch.setattr(community_tasks, "make_redis_lock_client", lambda _s: sentinel_lock)
    monkeypatch.setattr(community_tasks, "IngestionJobRepository", _FakeJobRepo)
    monkeypatch.setattr(community_tasks, "CommunityRepository", _FakeRepo)
    # No LLM summarizer in this smoke (keeps it network-free).
    monkeypatch.setattr(community_tasks, "make_summarizer", lambda _s, *, repo: None)

    result = await community_tasks._detect_async(str(uuid.uuid4()), _ORG)

    # The detect ran on the worker, with the per-graph lock client threaded through.
    assert _FakeRepo.detect_called is True
    assert _FakeRepo.built_with_lock is sentinel_lock
    # Job lifecycle: running first, then completed.
    statuses = [t["status"] for t in _FakeJobRepo.transitions]
    assert statuses == ["running", "completed"]
    # The completed row carries the community total (1 + 2 across the two honest levels).
    assert result["status"] == "completed"
    assert result["total_communities"] == 3


class _FakeEngine:
    async def dispose(self) -> None:
        return None


class _FakeDriver:
    def close(self) -> None:
        return None
