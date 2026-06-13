"""Unit: the consolidation task's per-(org,graph) advisory lock semantics (#332, #303/#305 pattern).

A held lock (another consolidation mid-run) must SKIP — never double-merge — and must not even
open a Neo4j driver. A free lock runs the pass and releases the lock afterwards. Redis/Neo4j are
faked at the task module's seams (``make_redis_lock_client`` / ``make_neo4j_driver``).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_knowledge_graph_service.services.memory_service import (
    memory_consolidation_lock_key,
)
from oraclous_knowledge_graph_service.tasks import memory_tasks

pytestmark = pytest.mark.unit

_ORG = str(uuid.uuid4())
_GRAPH = str(uuid.uuid4())


class _FakeRedis:
    """SET-NX-EX semantics over a dict (the RedisLock duck-type)."""

    def __init__(self, held: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = dict(held or {})

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, key: str) -> None:
        self.store.pop(key, None)

    def close(self) -> None:  # pragma: no cover — interface completeness
        pass


def test_held_lock_skips_and_never_opens_a_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    key = memory_consolidation_lock_key(organisation_id=_ORG, graph_id=_GRAPH)
    fake = _FakeRedis(held={key: "someone-else"})
    monkeypatch.setattr(memory_tasks, "make_redis_lock_client", lambda settings: fake)

    def _boom(settings: Any) -> Any:
        raise AssertionError("a skipped consolidation must not open a Neo4j driver")

    monkeypatch.setattr(memory_tasks, "make_neo4j_driver", _boom)

    out = memory_tasks.consolidate_memories_task(_GRAPH, _ORG)
    assert out == {"graph_id": _GRAPH, "merged": 0, "skipped": "locked"}
    assert fake.store[key] == "someone-else"  # the foreign holder was never released


def test_free_lock_runs_the_pass_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(memory_tasks, "make_redis_lock_client", lambda settings: fake)

    class _FakeDriver:
        closed = False

        def close(self) -> None:
            self.closed = True

    driver = _FakeDriver()
    monkeypatch.setattr(memory_tasks, "make_neo4j_driver", lambda settings: driver)

    seen: dict[str, Any] = {}

    def _fake_pass(repo: Any, *, threshold: float, max_memories: int, now: Any = None) -> dict:
        seen["threshold"] = threshold
        seen["max_memories"] = max_memories
        return {"candidates": 0, "clusters": 0, "merged": 0}

    monkeypatch.setattr(memory_tasks, "run_consolidation", _fake_pass)

    out = memory_tasks.consolidate_memories_task(_GRAPH, _ORG)
    assert out["graph_id"] == _GRAPH and out["merged"] == 0
    assert seen["threshold"] == 0.92 and seen["max_memories"] == 2000  # config defaults
    assert driver.closed is True
    key = memory_consolidation_lock_key(organisation_id=_ORG, graph_id=_GRAPH)
    assert key not in fake.store  # released after the pass
