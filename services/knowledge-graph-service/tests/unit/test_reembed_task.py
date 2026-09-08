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
from collections.abc import Iterator
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_ORG = str(uuid.uuid4())
_GRAPH = str(uuid.uuid4())
_TARGET_ID = "openai:text-embedding-3-small:512"


@pytest.fixture(autouse=True)
def _settings_cache_is_never_inherited() -> Iterator[None]:
    """`get_settings` is `lru_cache`d, so a test that pins `KGS_EMBEDDER` would otherwise leak its
    choice into whatever runs next (and inherit whatever ran before). Cleared on both sides."""
    from oraclous_knowledge_graph_service.core.config import get_settings  # noqa: PLC0415

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


class _FakeDriver:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _wire_task(
    monkeypatch: pytest.MonkeyPatch,
    tasks: Any,
    *,
    embedder: str,
    credential: Any,
) -> tuple[_FakeRedis, _FakeDriver, dict[str, Any], list[dict[str, Any]]]:
    """Drive `reembed_chunks_task` with a free lock, a fake driver and a stubbed credential.

    `embedder` is pinned OUT LOUD by every caller rather than inherited from the code default: which
    branch this task takes depends entirely on whether a credential is REQUIRED, so a test that let
    the default decide would silently change meaning the next time the default moves — which is
    exactly how the old version of this test stopped exercising the branch it is named for.
    """
    from oraclous_knowledge_graph_service.core.config import get_settings  # noqa: PLC0415

    monkeypatch.setenv("KGS_EMBEDDER", embedder)
    get_settings.cache_clear()  # `_settings_cache_is_never_inherited` clears it again on teardown

    redis = _FakeRedis()
    monkeypatch.setattr(tasks, "make_redis_lock_client", lambda settings: redis)
    driver = _FakeDriver()
    monkeypatch.setattr(tasks, "make_neo4j_driver", lambda settings: driver)

    resolved: list[dict[str, Any]] = []

    async def _fake_credential_for_graph(*_a: object, **kw: object) -> Any:
        resolved.append(dict(kw))
        return credential

    monkeypatch.setattr(tasks, "credential_for_graph", _fake_credential_for_graph)

    seen: dict[str, Any] = {}

    def _fake_pass(repo: Any, embedder_obj: Any, *, embedder_id: str, embedding_dim: int) -> dict:
        seen["called"] = True
        seen["repo"] = repo
        seen["embedder"] = embedder_obj
        seen["embedder_id"] = embedder_id
        seen["embedding_dim"] = embedding_dim
        return {"candidates": 0, "reembedded": 0}

    monkeypatch.setattr(tasks, "run_reembed", _fake_pass)
    return redis, driver, seen, resolved


def test_free_lock_runs_the_pass_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path, end to end through the Celery wrapper: take the lock, resolve the
    ORGANISATION's own model credential (#949 Q1), build the embedder FROM it, run the pass against
    the target identity, and release the lock.

    Everything after "take the lock" used to be unproven. The previous version stubbed the
    credential to None and asserted only the driver, the lock and `reembedded == 0` — all of which
    hold just as well on the skip path, where `run_reembed` is never called at all. So nothing in
    the spec established that this task ever invokes the pass. It does now, and the assertions
    below cannot be satisfied by any branch that does not.
    """
    from oraclous_knowledge_graph_service.services.embedder import OpenAIEmbedder  # noqa: PLC0415
    from oraclous_knowledge_graph_service.services.model_credential import (  # noqa: PLC0415
        ModelCredential,
    )

    tasks = _tasks_module()
    credential = ModelCredential(api_key="test-key-not-a-real-secret", credential_id="cred-1")
    redis, driver, seen, resolved = _wire_task(
        monkeypatch, tasks, embedder="openai", credential=credential
    )

    out = tasks.reembed_chunks_task(_GRAPH, _ORG)

    # The pass actually ran — the assertion the old test was missing entirely.
    assert seen.get("called") is True
    assert seen["embedder_id"] == _TARGET_ID  # the target space, not whatever the chunks are in
    assert seen["embedding_dim"] == 512
    assert seen["repo"] is not None  # an org-scoped repository, not the bare driver

    # The credential was resolved for THIS organisation and graph, and the embedder was built from
    # it: under `openai`, `make_embedder` cannot return an `OpenAIEmbedder` without one — it
    # refuses instead — so the embedder's own type is the proof the credential reached it.
    assert [(kw["organisation_id"], kw["graph_id"]) for kw in resolved] == [
        (uuid.UUID(_ORG), uuid.UUID(_GRAPH))
    ]
    assert isinstance(seen["embedder"], OpenAIEmbedder)

    # The pass's own stats are what the task reports (#949 Q2: per-workspace completion). The skip
    # path carries no `candidates` key and does carry `skipped`, so this shape is branch-specific.
    assert out["graph_id"] == _GRAPH
    assert out["candidates"] == 0
    assert out["reembedded"] == 0
    assert "skipped" not in out

    assert driver.closed is True
    lock_key = _reembed_lock_key()
    key = lock_key(organisation_id=_ORG, graph_id=_GRAPH)
    assert key not in redis.store  # released after the pass


def test_no_resolvable_credential_skips_the_pass_and_still_releases_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The twin of the test above, and what makes its explicit pin mean something.

    The beat sweep visits every graph of every organisation, including organisations that have
    designated no model credential. Those must be skipped quietly — not raised once per graph per
    cadence forever — but skipped is not the same as run: nothing may be re-embedded, and the lock
    must still come back so the next sweep is not blocked by this one.
    """
    tasks = _tasks_module()
    redis, driver, seen, resolved = _wire_task(
        monkeypatch, tasks, embedder="openai", credential=None
    )

    out = tasks.reembed_chunks_task(_GRAPH, _ORG)

    assert out["skipped"] == "no_model_credential"
    assert out["reembedded"] == 0
    assert seen == {}  # the pass was never invoked — nothing was embedded without a credential
    assert resolved  # ...but resolution WAS attempted, so a later connection is picked up
    assert driver.closed is True
    lock_key = _reembed_lock_key()
    assert lock_key(organisation_id=_ORG, graph_id=_GRAPH) not in redis.store


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
