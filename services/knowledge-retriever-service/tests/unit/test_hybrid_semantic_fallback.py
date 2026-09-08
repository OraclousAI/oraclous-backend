"""#950 ruling Q3 (C4) — combined ("hybrid") mode falls back to the meaning-based list ALONE when
the word-search input carries no real ranking, rather than fusing a real ranking with noise.

Today `fulltext()` gives every hit a constant `score=1.0` (an index-free CONTAINS scan, tie-broken
only by `elementId` for determinism — see `retrieval_repository.py::fulltext`'s own comment). RRF
fusion in `RetrievalService.hybrid()` blends that constant-score list with the semantic ranking as
if it were real, which — once #643/#949 land and semantic search gets good — makes combined mode
LOOK worse than semantic alone and would be blamed on the new embedder (the ruling's own sequencing
warning). The fix: "is this list ranked?" is a value the REPOSITORY hands back next to the rows
(never a constant baked into the service), so the day #950's real full-text index lands, the same
service code starts fusing for real with no second edit.

`fulltext()` does not yet report a ranked/unranked flag and `hybrid()` does not yet fall back on
it — every not-yet-built seam use is FUNCTION-LOCAL (`.claude/rules/tests-seam-imports.md`); this
file hard-fails RED until the `[impl]` lands.
"""

from __future__ import annotations

import pytest
from oraclous_governance import OrganisationContext, PrincipalType, use_organisation_context

pytestmark = pytest.mark.unit

_ORG = __import__("uuid").UUID("00000000-0000-0000-0000-00000000050a")


def _ctx():
    return use_organisation_context(
        OrganisationContext(
            organisation_id=_ORG, principal_id=_ORG, principal_type=PrincipalType.USER
        )
    )


class _FakeRecord:
    def __init__(self, d: dict) -> None:
        self._d = d

    def data(self) -> dict:
        return self._d


class _FakeDriver:
    """Answers every `execute_query` call with the next canned row-set, in call order."""

    def __init__(self, rows_by_call: list[list[dict]]) -> None:
        self._rows_by_call = rows_by_call
        self.calls = 0

    def execute_query(self, cypher, **kw):
        rows = self._rows_by_call[min(self.calls, len(self._rows_by_call) - 1)]
        self.calls += 1
        return ([_FakeRecord(r) for r in rows], None, None)


def _row(cid: str, text: str, score: float = 1.0) -> dict:
    return {"id": cid, "labels": ["Chunk"], "props": {"text": text}, "score": score}


def _service(rows_by_call: list[list[dict]]):
    from oraclous_embedding import HashingEmbedder  # noqa: PLC0415
    from oraclous_knowledge_retriever_service.services.retrieval_service import (  # noqa: PLC0415
        RetrievalService,
    )

    return RetrievalService(_FakeDriver(rows_by_call), HashingEmbedder(dim=8))


# --- the repository reports whether a list is genuinely ranked --------------------------------


def test_fulltext_reports_unranked_alongside_the_rows() -> None:
    """Today's index-free CONTAINS scan gives every hit the same constant score — a real
    relevance ranking, this is not. The repository must say so, not leave the service to assume."""
    from oraclous_knowledge_retriever_service.repositories.retrieval_repository import (  # noqa: PLC0415, E501
        RetrievalRepository,
    )

    driver = _FakeDriver([[_row("c1", "ada"), _row("c2", "babbage")]])
    repo = RetrievalRepository(driver, organisation_id=str(_ORG))

    result = repo.fulltext_ranked(graph_id="g1", query="ada", top_k=10)

    assert result.ranked is False
    assert [r["props"]["text"] for r in result.rows] == ["ada", "babbage"]


# --- the service falls back to semantic-only when the word-search input is unranked -----------


async def test_hybrid_returns_the_semantic_list_alone_when_fulltext_is_unranked() -> None:
    semantic_rows = [_row("c1", "ada", score=0.9), _row("c2", "babbage", score=0.4)]
    fulltext_rows = [_row("c3", "unrelated hit", score=1.0)]
    svc = _service([semantic_rows, fulltext_rows])

    with _ctx():
        results = await svc.hybrid(graph_id="g1", query="ada", top_k=10)

    # the semantic list, verbatim — c3 (only reachable via the unranked fulltext input) never
    # appears, and the order is the semantic order, not an RRF blend
    assert [r["id"] for r in results] == ["c1", "c2"]


async def test_hybrid_does_not_refuse_on_an_unranked_fulltext_input() -> None:
    """The ruling is explicit: it degrades, it does not refuse — a caller asking for the best
    results should still get the best available results."""
    semantic_rows = [_row("c1", "ada", score=0.9)]
    fulltext_rows = [_row("c3", "unrelated hit", score=1.0)]
    svc = _service([semantic_rows, fulltext_rows])

    with _ctx():
        results = await svc.hybrid(graph_id="g1", query="ada", top_k=10)

    assert len(results) == 1  # no exception raised


async def test_hybrid_marks_the_degraded_fallback_as_observable_fusion_mode() -> None:
    """The ruling: "the fallback must be observable" — never an invisible behaviour change. No
    sibling response field (a new envelope would be a §12 cross-repo Contract); `properties`
    already carries `score`/`rrf_score`/`precedence_tier`, so `fusion_mode` joins them there."""
    semantic_rows = [_row("c1", "ada", score=0.9)]
    fulltext_rows = [_row("c3", "unrelated hit", score=1.0)]
    svc = _service([semantic_rows, fulltext_rows])

    with _ctx():
        results = await svc.hybrid(graph_id="g1", query="ada", top_k=10)

    assert all(r["properties"]["fusion_mode"] == "semantic_only" for r in results)


async def test_hybrid_never_returns_an_rrf_score_when_it_degraded() -> None:
    """An `rrf_score` on a degraded result would be a lie — there was no fusion."""
    semantic_rows = [_row("c1", "ada", score=0.9)]
    fulltext_rows = [_row("c3", "unrelated hit", score=1.0)]
    svc = _service([semantic_rows, fulltext_rows])

    with _ctx():
        results = await svc.hybrid(graph_id="g1", query="ada", top_k=10)

    assert all("rrf_score" not in r["properties"] for r in results)


async def test_todays_contains_scan_always_reports_unranked() -> None:
    """Pins the honest baseline: C4 ships no real index (that is #950's separate work), so the
    CONTAINS-scan repository method must report `ranked=False` for every query — never a heuristic
    over the returned scores (which a constant-1.0 scan could accidentally satisfy)."""
    from oraclous_knowledge_retriever_service.repositories.retrieval_repository import (  # noqa: PLC0415, E501
        RetrievalRepository,
    )

    driver = _FakeDriver([[_row("c1", "ada", score=1.0), _row("c2", "babbage", score=1.0)]])
    repo = RetrievalRepository(driver, organisation_id=str(_ORG))
    assert repo.fulltext_ranked(graph_id="g1", query="ada", top_k=10).ranked is False


async def test_hybrid_fuses_for_real_once_the_repository_reports_ranked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of moving the flag into the repository rather than a service constant: once
    #950's real index makes `fulltext_ranked` report `ranked=True`, `hybrid()` must fuse
    WITHOUT a second edit to the service. Proven here by monkeypatching the repository boundary
    directly — decoupled from how `ranked` eventually gets computed, #950's concern, not this
    one's."""
    import oraclous_knowledge_retriever_service.repositories.retrieval_repository as repo_module  # noqa: PLC0415, E501

    semantic_rows = [_row("c1", "ada", score=0.9), _row("c2", "babbage", score=0.4)]
    fulltext_rows = [_row("c2", "babbage", score=3.1), _row("c1", "ada", score=1.2)]
    svc = _service([semantic_rows])  # only the semantic Cypher call is driven through the fake

    class _RankedResult:
        def __init__(self, rows: list[dict]) -> None:
            self.rows = rows
            self.ranked = True

    def _fake_fulltext_ranked(self, *, graph_id, query, top_k):  # noqa: ANN001, ANN202
        return _RankedResult(fulltext_rows)

    monkeypatch.setattr(repo_module.RetrievalRepository, "fulltext_ranked", _fake_fulltext_ranked)

    with _ctx():
        results = await svc.hybrid(graph_id="g1", query="ada", top_k=10)

    # a real fusion happened: both ids present, an rrf_score stamped, no degraded marker
    assert {r["id"] for r in results} == {"c1", "c2"}
    assert all("rrf_score" in r["properties"] for r in results)
    assert all(r["properties"].get("fusion_mode") != "semantic_only" for r in results)
