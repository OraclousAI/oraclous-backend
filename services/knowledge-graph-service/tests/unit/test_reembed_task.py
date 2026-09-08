"""#949 ruling Q2 (C6) — automatic re-embed. When a workspace moves from the keyless hashing
embedder to a real one, the platform re-embeds that workspace's stored chunks itself: no button the
user has to find, and the workspace must not sit searching against vectors from a different space
while it waits. Restartable and idempotent; per-workspace completion reported.

Mirrors the proven `tasks/memory_tasks.py` shape exactly: a pure `run_reembed(repo, embedder, ...)`
core (integration-testable against real substrate with no worker/broker — see
`test_reembed_chunks_substrate.py`), a per-graph Celery task under the same
per-(org,graph) advisory Redis lock pattern (#303/#305), and a beat dispatcher that fans out over
every graph still holding a stale chunk.

`tasks/reembed_tasks.py` does not exist yet. Every not-yet-built seam use is FUNCTION-LOCAL
(`.claude/rules/tests-seam-imports.md`); this file hard-fails RED with `ModuleNotFoundError` until
the `[impl]` lands.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_ORG = str(uuid.uuid4())
_GRAPH = str(uuid.uuid4())
_TARGET_ID = "openai:text-embedding-3-small:512"


def _tasks_module():
    from oraclous_knowledge_graph_service.tasks import reembed_tasks  # noqa: PLC0415

    return reembed_tasks


def _reembed_lock_key():
    # Mirrors `memory_consolidation_lock_key` living in `services/memory_service.py` — the lock
    # key is a small, dependency-free helper, kept out of the tasks module so both the task and
    # (eventually) an admin/status endpoint can compute the same key without importing Celery.
    from oraclous_knowledge_graph_service.services.reembed_service import (  # noqa: PLC0415
        reembed_lock_key,
    )

    return reembed_lock_key


# --- run_reembed: pure, batch-oriented, only ever touches STALE chunks -------------------------


class _FakeReembedRepo:
    def __init__(self, stale: list[dict]) -> None:
        self._stale = list(stale)
        self.writes: list[dict] = []

    def list_chunks_needing_reembed(self, *, embedder_id: str, limit: int) -> list[dict]:
        return self._stale[:limit]

    def write_embedding(
        self, *, chunk_id: str, embedding: list[float], embedder_id: str, embedding_dim: int
    ) -> None:
        self.writes.append(
            {
                "chunk_id": chunk_id,
                "embedding": embedding,
                "embedder_id": embedder_id,
                "embedding_dim": embedding_dim,
            }
        )
        # simulate the write actually landing: the chunk stops being stale
        self._stale = [c for c in self._stale if c["id"] != chunk_id]


class _FakeEmbedder:
    dim = 512

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t))] * self.dim for t in texts]


def _run_reembed():
    # Mirrors `run_consolidation` living directly in `tasks/memory_tasks.py` (the lock-free core,
    # so the integration test drives it against real substrate with no worker/broker).
    tasks = _tasks_module()
    return tasks.run_reembed


def test_only_stale_chunks_are_rewritten() -> None:
    run_reembed = _run_reembed()
    repo = _FakeReembedRepo([{"id": "c1", "text": "hello"}, {"id": "c2", "text": "world!"}])

    stats = run_reembed(repo, _FakeEmbedder(), embedder_id=_TARGET_ID, embedding_dim=512)

    assert {w["chunk_id"] for w in repo.writes} == {"c1", "c2"}
    assert all(w["embedder_id"] == _TARGET_ID for w in repo.writes)
    assert stats["reembedded"] == 2


def test_a_completed_workspace_is_a_no_op() -> None:
    """Idempotent: nothing left stale -> nothing rewritten, no wasted embed calls."""
    run_reembed = _run_reembed()
    repo = _FakeReembedRepo([])

    stats = run_reembed(repo, _FakeEmbedder(), embedder_id=_TARGET_ID, embedding_dim=512)

    assert repo.writes == []
    assert stats["reembedded"] == 0


def test_a_crashed_run_resumes_by_re_reading_only_what_still_does_not_match() -> None:
    """Restartable: `run_reembed` never assumes it starts from a clean slate — a second call sees
    only what the first call left stale (here: nothing, because the fake repo drops a chunk from
    `_stale` the moment it is written, exactly mirroring the real repository's identity filter)."""
    run_reembed = _run_reembed()
    repo = _FakeReembedRepo([{"id": "c1", "text": "hello"}])

    first = run_reembed(repo, _FakeEmbedder(), embedder_id=_TARGET_ID, embedding_dim=512)
    second = run_reembed(repo, _FakeEmbedder(), embedder_id=_TARGET_ID, embedding_dim=512)

    assert first["reembedded"] == 1
    assert second["reembedded"] == 0
    assert len(repo.writes) == 1  # never rewritten twice


def test_reembedding_runs_in_bounded_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace's chunk count is unbounded; `run_reembed` must fetch/embed in bounded batches,
    never one unbounded `list_chunks_needing_reembed(limit=<all of it>)` call (the #305/#332
    pattern every other sweep in this service already follows)."""
    run_reembed = _run_reembed()
    many = [{"id": f"c{i}", "text": f"chunk {i}"} for i in range(5)]
    repo = _FakeReembedRepo(many)

    calls: list[int] = []
    original = repo.list_chunks_needing_reembed

    def _tracking(*, embedder_id: str, limit: int) -> list[dict]:
        calls.append(limit)
        return original(embedder_id=embedder_id, limit=limit)

    repo.list_chunks_needing_reembed = _tracking  # type: ignore[method-assign]

    run_reembed(repo, _FakeEmbedder(), embedder_id=_TARGET_ID, embedding_dim=512, batch_size=2)

    assert all(limit <= 2 for limit in calls), calls
    assert len(repo.writes) == 5


# --- the Celery task: advisory per-(org,graph) lock (#303/#305 pattern) ------------------------


class _FakeRedis:
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
    tasks = _tasks_module()
    lock_key = _reembed_lock_key()
    key = lock_key(organisation_id=_ORG, graph_id=_GRAPH)
    fake = _FakeRedis(held={key: "someone-else"})
    monkeypatch.setattr(tasks, "make_redis_lock_client", lambda settings: fake)

    def _boom(settings: Any) -> Any:
        raise AssertionError("a skipped re-embed must not open a Neo4j driver")

    monkeypatch.setattr(tasks, "make_neo4j_driver", _boom)

    out = tasks.reembed_chunks_task(_GRAPH, _ORG)
    assert out["graph_id"] == _GRAPH
    assert out.get("skipped") == "locked"
    assert fake.store[key] == "someone-else"


def test_free_lock_runs_the_pass_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    tasks = _tasks_module()
    fake = _FakeRedis()
    monkeypatch.setattr(tasks, "make_redis_lock_client", lambda settings: fake)

    class _FakeDriver:
        closed = False

        def close(self) -> None:
            self.closed = True

    driver = _FakeDriver()
    monkeypatch.setattr(tasks, "make_neo4j_driver", lambda settings: driver)

    async def _fake_credential_for_graph(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(tasks, "credential_for_graph", _fake_credential_for_graph)

    seen: dict[str, Any] = {}

    def _fake_pass(repo: Any, embedder: Any, *, embedder_id: str, embedding_dim: int) -> dict:
        seen["embedder_id"] = embedder_id
        return {"candidates": 0, "reembedded": 0}

    monkeypatch.setattr(tasks, "run_reembed", _fake_pass)

    out = tasks.reembed_chunks_task(_GRAPH, _ORG)

    assert out["graph_id"] == _GRAPH
    assert out["reembedded"] == 0
    assert driver.closed is True
    lock_key = _reembed_lock_key()
    key = lock_key(organisation_id=_ORG, graph_id=_GRAPH)
    assert key not in fake.store  # released after the pass


# --- the beat dispatcher: bounded fan-out, mirrors enumerate_memory_graphs --------------------


def test_beat_dispatcher_fans_out_one_task_per_graph_needing_reembed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = _tasks_module()

    class _FakeDriver:
        def close(self) -> None:
            pass

    monkeypatch.setattr(tasks, "make_neo4j_driver", lambda settings: _FakeDriver())
    monkeypatch.setattr(
        tasks, "enumerate_graphs_needing_reembed", lambda driver, **kw: [(_ORG, _GRAPH)]
    )

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        tasks.reembed_chunks_task,
        "delay",
        lambda graph_id, org_id: dispatched.append((graph_id, org_id)),
    )

    out = tasks.reembed_all_graphs_task()

    assert dispatched == [(_GRAPH, _ORG)]
    assert out["dispatched"] == 1
