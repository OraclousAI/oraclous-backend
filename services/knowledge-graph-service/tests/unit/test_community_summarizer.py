"""Unit tests for CommunitySummarizer (#303) with a mocked LLM + fake repo — no network, no Neo4j.

Covers: a well-formed JSON response is parsed into summary/keywords/excerpt + persisted with
``source='llm'`` and the model name; a malformed/empty response degrades to a deterministic
member-derived fallback that is DISTINGUISHABLE (``source='fallback'``, ``summary_model=None``) so a
reader never mistakes it for a real summary (never raises); concurrency is bounded; one failing
community does not sink the batch; skip-existing (only_unsummarized) + force; the inline cap defers.
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.domain.community import Community, CommunityMember
from oraclous_knowledge_graph_service.services.community_summarizer import (
    CommunitySummarizer,
    _parse_summary,
)

pytestmark = pytest.mark.unit


class _FakeLLM:
    """Returns a queued response per call; records the prompts it saw."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.max_in_flight = 0
        self._in_flight = 0

    async def complete_json(self, *, system: str, user: str) -> str:  # noqa: ARG002
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            self.calls.append(user)
            resp = self._responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp
        finally:
            self._in_flight -= 1


class _FakeRepo:
    def __init__(self, communities: list[Community]) -> None:
        self._communities = communities
        self.persisted: list[dict] = []
        self.list_calls: list[dict] = []

    def list_communities(
        self,
        *,
        graph_id: str,
        level,
        min_entities,
        only_unsummarized=False,  # noqa: ANN001, ARG002
    ):
        self.list_calls.append({"level": level, "only_unsummarized": only_unsummarized})
        return self._communities

    def members_with_relationships(self, *, graph_id, community_id, member_limit, rel_limit):  # noqa: ANN001, ARG002
        members = [
            CommunityMember(entity_id="e1", entity_name="Alice", entity_type="Person"),
            CommunityMember(entity_id="e2", entity_name="Bob", entity_type="Person"),
        ]
        rels = [{"src": "Alice", "rel": "KNOWS", "tgt": "Bob"}]
        return members, rels

    def set_summary(self, **kwargs):  # noqa: ANN003
        self.persisted.append(kwargs)
        return True


def _community(cid: str) -> Community:
    return Community(community_id=cid, kind="entity", level=0, entity_count=2, status="active")


def test_parse_summary_wellformed() -> None:
    raw = (
        '{"summary": "A friendship cluster.", "keywords": ["Alice", "Bob"], '
        '"excerpt": "Alice knows Bob."}'
    )
    members = [CommunityMember(entity_id="e1", entity_name="Alice", entity_type="Person")]
    summary, keywords, excerpt, source = _parse_summary(raw, fallback_members=members)
    assert summary == "A friendship cluster."
    assert keywords == ["Alice", "Bob"]
    assert excerpt == "Alice knows Bob."
    assert source == "llm"  # a real model answer


def test_parse_summary_malformed_falls_back_and_is_marked() -> None:
    members = [
        CommunityMember(entity_id="e1", entity_name="Alice", entity_type="Person"),
        CommunityMember(entity_id="e2", entity_name="Bob", entity_type="Person"),
    ]
    summary, keywords, excerpt, source = _parse_summary("not json at all", fallback_members=members)
    # Deterministic fallback derived from members — never raises, always usable.
    assert "Alice" in summary and "Bob" in summary
    assert keywords  # non-empty
    assert excerpt
    # ...and it is MARKED as a fallback so a reader can tell it from a real summary.
    assert source == "fallback"


def test_parse_summary_empty_summary_is_fallback() -> None:
    members = [CommunityMember(entity_id="e1", entity_name="Alice", entity_type="Person")]
    # Well-formed JSON but an EMPTY summary field — still a degrade, must be marked fallback.
    raw = '{"summary": "", "keywords": [], "excerpt": ""}'
    _s, _k, _e, source = _parse_summary(raw, fallback_members=members)
    assert source == "fallback"


async def test_fallback_persists_distinguishably(monkeypatch) -> None:
    # A malformed LLM response is persisted WITH source='fallback' and NO model name, so it is not
    # indistinguishable from a real summary (the #5 finding).
    repo = _FakeRepo([_community("community_a")])
    llm = _FakeLLM(["totally not json"])
    summarizer = CommunitySummarizer(
        repo=repo,  # type: ignore[arg-type]
        llm=llm,
        model_name="test-model",
        max_concurrency=1,
    )
    outcome = await summarizer.summarize_graph(graph_id="g1")
    assert outcome.status == "completed"
    results = outcome.results
    assert len(results) == 1
    assert results[0].source == "fallback"
    persisted = repo.persisted[0]
    assert persisted["summary_source"] == "fallback"
    assert persisted["summary_model"] is None  # a fallback never reached the model


async def test_summarize_graph_persists_each() -> None:
    repo = _FakeRepo([_community("community_a"), _community("community_b")])
    llm = _FakeLLM(
        [
            '{"summary": "Cluster A.", "keywords": ["x"], "excerpt": "e"}',
            '{"summary": "Cluster B.", "keywords": ["y"], "excerpt": "f"}',
        ]
    )
    summarizer = CommunitySummarizer(
        repo=repo,  # type: ignore[arg-type]
        llm=llm,
        model_name="test-model",
        max_concurrency=2,
    )
    outcome = await summarizer.summarize_graph(graph_id="g1")
    assert outcome.status == "completed"
    assert len(outcome.results) == 2
    assert len(repo.persisted) == 2
    # Provenance: the model name + source='llm' flow into every real persisted summary.
    assert all(p["summary_model"] == "test-model" for p in repo.persisted)
    assert all(p["summary_source"] == "llm" for p in repo.persisted)
    # Default run is cost-aware: it asks the repo for only un-summarised communities.
    assert repo.list_calls[0]["only_unsummarized"] is True


async def test_skip_existing_by_default_and_force_resummarises() -> None:
    repo = _FakeRepo([_community("community_a")])
    llm = _FakeLLM(['{"summary": "s", "keywords": ["k"], "excerpt": "e"}'] * 2)
    summarizer = CommunitySummarizer(
        repo=repo,  # type: ignore[arg-type]
        llm=llm,
        model_name="m",
        max_concurrency=1,
    )
    await summarizer.summarize_graph(graph_id="g1")
    await summarizer.summarize_graph(graph_id="g1", force=True)
    # Default → only_unsummarized True (skip existing); force → False (re-summarise all).
    assert repo.list_calls[0]["only_unsummarized"] is True
    assert repo.list_calls[1]["only_unsummarized"] is False


async def test_inline_cap_defers_large_batches() -> None:
    repo = _FakeRepo([_community(f"community_{i}") for i in range(5)])
    llm = _FakeLLM(['{"summary": "s", "keywords": ["k"], "excerpt": "e"}'] * 5)
    summarizer = CommunitySummarizer(
        repo=repo,  # type: ignore[arg-type]
        llm=llm,
        model_name="m",
        max_concurrency=2,
    )
    # 5 candidates exceed the inline cap of 2 → deferred (no LLM calls, nothing persisted), and the
    # outcome is DISTINGUISHABLE from a completed empty run (status='deferred' + candidate count).
    outcome = await summarizer.summarize_graph(graph_id="g1", max_communities=2)
    assert outcome.results == []
    assert outcome.status == "deferred"
    assert outcome.deferred_count == 5
    assert repo.persisted == []
    assert llm.calls == []


async def test_completed_empty_run_is_distinguishable_from_deferred() -> None:
    # Nothing to summarise (no candidates) → a COMPLETED run with no results — distinct from the
    # capped/deferred case above (both would otherwise just show summarized=0).
    repo = _FakeRepo([])
    llm = _FakeLLM([])
    summarizer = CommunitySummarizer(
        repo=repo,  # type: ignore[arg-type]
        llm=llm,
        model_name="m",
        max_concurrency=1,
    )
    outcome = await summarizer.summarize_graph(graph_id="g1", max_communities=2)
    assert outcome.results == []
    assert outcome.status == "completed"
    assert outcome.deferred_count == 0


async def test_one_failing_community_does_not_sink_batch() -> None:
    repo = _FakeRepo([_community("community_a"), _community("community_b")])
    ok = '{"summary": "ok", "keywords": ["y"], "excerpt": "f"}'
    llm = _FakeLLM([RuntimeError("LLM down"), ok])
    summarizer = CommunitySummarizer(
        repo=repo,  # type: ignore[arg-type]
        llm=llm,
        model_name="test-model",
        max_concurrency=1,
    )
    outcome = await summarizer.summarize_graph(graph_id="g1")
    # One failed, one succeeded — the batch survives.
    assert len(outcome.results) == 1
    assert len(repo.persisted) == 1


async def test_concurrency_is_bounded() -> None:
    communities = [_community(f"community_{i}") for i in range(6)]
    repo = _FakeRepo(communities)
    llm = _FakeLLM(['{"summary": "s", "keywords": ["k"], "excerpt": "e"}'] * 6)
    summarizer = CommunitySummarizer(
        repo=repo,  # type: ignore[arg-type]
        llm=llm,
        model_name="m",
        max_concurrency=2,
    )
    await summarizer.summarize_graph(graph_id="g1")
    # Never more than the configured concurrency in flight at once.
    assert llm.max_in_flight <= 2
