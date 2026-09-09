"""Unit: a completed re-embed pass invalidates the retriever's cached reads (#949 Q2 / #308).

Review finding on the #643 implementation: `run_reembed` rewrote every chunk's vector but never
bumped the per-graph generation counter, which a completed ingest has always bumped. The retriever
folds that counter into its cache key, so without the bump a semantic or hybrid response computed
BEFORE the flip kept being served for the rest of the cache TTL — stale-space output reaching the
user through the cache rather than the store, during exactly the transition the automatic re-embed
exists to make invisible.

The bump is advisory in both directions, and both are pinned here: it only happens when the pass
actually rewrote something, and a Redis fault can never fail a pass whose vectors are already
written.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_ORG = str(uuid.uuid4())
_GRAPH = str(uuid.uuid4())


def _tasks_module():
    from oraclous_knowledge_graph_service.tasks import reembed_tasks  # noqa: PLC0415

    return reembed_tasks


class _FakeRedis:
    """The advisory lock client: enough of it for acquire/release to work."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, *keys: str) -> int:
        return sum(bool(self.store.pop(k, None)) for k in keys)

    def eval(self, _script: str, _numkeys: int, key: str, value: str) -> int:
        if self.store.get(key) == value:
            del self.store[key]
            return 1
        return 0

    def close(self) -> None:
        pass


class _FakeDriver:
    def close(self) -> None:
        pass


def _wire(monkeypatch: pytest.MonkeyPatch, tasks, *, reembedded: int) -> list[dict]:
    """Drive the task down its RUN branch (a credential resolves), recording every bump."""
    monkeypatch.setattr(tasks, "make_redis_lock_client", lambda settings: _FakeRedis())
    monkeypatch.setattr(tasks, "make_neo4j_driver", lambda settings: _FakeDriver())

    async def _credential(*_a: object, **_kw: object) -> object:
        return object()  # the org HAS a model credential, so the pass runs rather than skipping

    monkeypatch.setattr(tasks, "credential_for_graph", _credential)
    monkeypatch.setattr(tasks, "ReembedRepository", lambda *a, **kw: object())
    monkeypatch.setattr(
        tasks,
        "run_reembed",
        lambda *a, **kw: {"candidates": reembedded, "reembedded": reembedded},
    )

    bumps: list[dict] = []

    class _Generation:
        @staticmethod
        def bump_for(*, redis_url: str, organisation_id: str, graph_id: str) -> None:
            bumps.append({"organisation_id": organisation_id, "graph_id": graph_id})

    monkeypatch.setattr(tasks, "GraphGenerationRepository", _Generation)
    return bumps


def test_a_pass_that_rewrote_chunks_invalidates_that_graphs_cached_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = _tasks_module()
    bumps = _wire(monkeypatch, tasks, reembedded=7)

    out = tasks.reembed_chunks_task(_GRAPH, _ORG)

    assert out["reembedded"] == 7
    # Scoped to the (org, graph) whose vectors moved — never a blanket flush.
    assert bumps == [{"organisation_id": _ORG, "graph_id": _GRAPH}]


def test_a_pass_that_found_nothing_stale_invalidates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep visits every graph on a cadence. A graph already in the target space has
    invalidated nothing, and bumping it would throw away a warm cache for no reason."""
    tasks = _tasks_module()
    bumps = _wire(monkeypatch, tasks, reembedded=0)

    out = tasks.reembed_chunks_task(_GRAPH, _ORG)

    assert out["reembedded"] == 0
    assert bumps == []


def test_a_redis_fault_cannot_fail_a_pass_whose_vectors_are_already_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalidation is a hint, not a write. Raising here would report a pass that genuinely
    re-embedded the workspace as failed, and re-run it on the next sweep for nothing."""
    tasks = _tasks_module()
    _wire(monkeypatch, tasks, reembedded=3)

    class _BrokenGeneration:
        @staticmethod
        def bump_for(**_kw: Any) -> None:
            raise RuntimeError("redis is unreachable")

    monkeypatch.setattr(tasks, "GraphGenerationRepository", _BrokenGeneration)

    out = tasks.reembed_chunks_task(_GRAPH, _ORG)

    assert out["reembedded"] == 3  # the pass still reports what it actually wrote
