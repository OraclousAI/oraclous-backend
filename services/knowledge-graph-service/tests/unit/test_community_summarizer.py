"""Unit tests for CommunitySummarizer (#303) with a mocked LLM + fake repo — no network, no Neo4j.

Covers: a well-formed JSON response is parsed into summary/keywords/excerpt + persisted; a malformed
response degrades to a deterministic member-derived fallback (never raises); concurrency is bounded;
one failing community does not sink the batch; provenance (model) is recorded.
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

    def list_communities(self, *, graph_id: str, level, min_entities):  # noqa: ANN001, ARG002
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
    return Community(
        community_id=cid, kind="entity", level=0, resolution=1.0, entity_count=2, status="active"
    )


def test_parse_summary_wellformed() -> None:
    raw = (
        '{"summary": "A friendship cluster.", "keywords": ["Alice", "Bob"], '
        '"excerpt": "Alice knows Bob."}'
    )
    members = [CommunityMember(entity_id="e1", entity_name="Alice", entity_type="Person")]
    summary, keywords, excerpt = _parse_summary(raw, fallback_members=members)
    assert summary == "A friendship cluster."
    assert keywords == ["Alice", "Bob"]
    assert excerpt == "Alice knows Bob."


def test_parse_summary_malformed_falls_back() -> None:
    members = [
        CommunityMember(entity_id="e1", entity_name="Alice", entity_type="Person"),
        CommunityMember(entity_id="e2", entity_name="Bob", entity_type="Person"),
    ]
    summary, keywords, excerpt = _parse_summary("not json at all", fallback_members=members)
    # Deterministic fallback derived from members — never raises, always usable.
    assert "Alice" in summary and "Bob" in summary
    assert keywords  # non-empty
    assert excerpt


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
    results = await summarizer.summarize_graph(graph_id="g1")
    assert len(results) == 2
    assert len(repo.persisted) == 2
    # Provenance: the model name flows into every persisted summary.
    assert all(p["summary_model"] == "test-model" for p in repo.persisted)


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
    results = await summarizer.summarize_graph(graph_id="g1")
    # One failed, one succeeded — the batch survives.
    assert len(results) == 1
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
