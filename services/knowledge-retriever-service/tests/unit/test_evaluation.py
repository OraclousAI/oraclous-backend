"""Unit tests for the native RAGAS-style evaluation service (#331) — a FAKE judge, no network.

Covers: per-metric score computation (incl. partial-support fractions + 4-dp rounding), metric
subset selection, ground_truth-gated context_recall, fail-soft (judge raises → null + warning;
malformed JSON → null + warning; non-finite scores rejected), partial verdict-batch failures
(fraction over successes, majority-fail null), the overall deadline (partial results + genuine
cancellation of in-flight judge calls), the answer-generation branch (incl. generation failure),
caps (claims, once-applied context set, per-request concurrency, process-level capacity), graph
existence, the no-key judge factory, and the lifespan judge singleton (#333).
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from oraclous_knowledge_retriever_service.core.config import Settings, get_settings
from oraclous_knowledge_retriever_service.services import evaluation_service as ev
from oraclous_knowledge_retriever_service.services.eval_judge import (
    EvalJudge,
    OpenAIEvalJudge,
    make_judge,
)
from oraclous_knowledge_retriever_service.services.evaluation_service import (
    EvaluationCapacityExceeded,
    EvaluationService,
    GraphNotFound,
    NoValidMetrics,
)

pytestmark = pytest.mark.unit

_GENERATED = "Ada Lovelace wrote the first computer program."


class FakeJudge:
    """Dispatches canned JSON on the system prompt (the stable per-step contract); records calls.

    A canned value may be a string, a callable(user) → string (per-item verdicts), a callable
    that raises (failure injection), or a callable returning an awaitable (hangs / counters).
    """

    def __init__(self, json_responses: dict | None = None, text_response=_GENERATED) -> None:
        self._json = dict(json_responses or {})
        self._text = text_response
        self.json_calls: list[tuple[str, str]] = []
        self.text_calls: list[tuple[str, str]] = []

    async def complete_json(self, *, system: str, user: str) -> str:
        self.json_calls.append((system, user))
        handler = self._json[system]
        if callable(handler):
            result = handler(user)
            if inspect.isawaitable(result):
                return await result
            return result
        return handler

    async def complete_text(self, *, system: str, user: str) -> str:
        self.text_calls.append((system, user))
        if isinstance(self._text, Exception):
            raise self._text
        return self._text

    def calls_for(self, system: str) -> list[str]:
        return [user for sys_, user in self.json_calls if sys_ == system]


def _happy_responses(**overrides) -> dict:
    responses = {
        ev.CLAIMS_SYSTEM: '{"claims": ["claim one", "claim two", "claim three"]}',
        ev.CLAIM_VERDICT_SYSTEM: '{"supported": true}',
        ev.RELEVANCE_SYSTEM: '{"score": 0.9}',
        ev.PRECISION_SYSTEM: '{"relevant": true}',
        ev.STATEMENTS_SYSTEM: '{"statements": ["fact one", "fact two"]}',
        ev.RECALL_VERDICT_SYSTEM: '{"attributable": true}',
    }
    responses.update(overrides)
    return responses


def _nodes() -> list[dict]:
    return [
        {
            "id": "4:x:1",
            "type": "Chunk",
            "properties": {"text": "ada lovelace wrote the first program", "rrf_score": 0.03},
        },
        {
            "id": "4:x:2",
            "type": "Chunk",
            "properties": {"text": "charles babbage designed the analytical engine", "score": 0.5},
        },
        {"id": "4:x:3", "type": "Chunk", "properties": {"text": "the engine was mechanical"}},
        {"id": "4:x:4", "type": "Chunk", "properties": {"text": "grace hopper built a compiler"}},
    ]


class FakeRetrieval:
    def __init__(self, nodes: list[dict] | None = None, exists: bool = True) -> None:
        self._nodes = _nodes() if nodes is None else nodes
        self._exists = exists

    async def graph_exists(self, *, graph_id: str) -> bool:
        return self._exists

    async def hybrid(self, *, graph_id: str, query: str, top_k: int) -> list[dict]:
        return self._nodes[:top_k]


def _service(judge, retrieval=None, **kwargs) -> EvaluationService:
    return EvaluationService(retrieval=retrieval or FakeRetrieval(), judge=judge, **kwargs)


async def _evaluate(service, **kwargs) -> dict:
    defaults = {
        "graph_id": "g1",
        "question": "Who wrote the first computer program?",
        "answer": "Ada Lovelace wrote it.",
        "ground_truth": None,
        "metrics": None,
    }
    defaults.update(kwargs)
    return await service.evaluate(**defaults)


# --- score computation per metric ---------------------------------------------------------------


async def test_faithfulness_partial_support_fraction_and_threshold() -> None:
    # 2 of 3 claims supported → 0.6667 (4 dp); below the 0.7 default → not grounded.
    judge = FakeJudge(
        _happy_responses(
            **{
                ev.CLAIM_VERDICT_SYSTEM: lambda user: (
                    '{"supported": false}' if "claim three" in user else '{"supported": true}'
                )
            }
        )
    )
    result = await _evaluate(_service(judge), metrics=["faithfulness"])
    assert result["scores"]["faithfulness"] == 0.6667
    assert result["metrics_computed"] == ["faithfulness"]
    assert result["overall"] == 0.6667
    assert result["is_grounded"] is False


async def test_all_metrics_happy_path_overall_and_grounded() -> None:
    judge = FakeJudge(_happy_responses())
    result = await _evaluate(_service(judge), ground_truth="Ada Lovelace wrote it first.")
    scores = result["scores"]
    assert scores == {
        "faithfulness": 1.0,
        "answer_relevance": 0.9,
        "context_precision": 1.0,
        "context_recall": 1.0,
    }
    assert result["overall"] == round((1.0 + 0.9 + 1.0 + 1.0) / 4, 4)
    assert result["metrics_computed"] == [
        "faithfulness",
        "answer_relevance",
        "context_precision",
        "context_recall",
    ]
    assert result["is_grounded"] is True
    assert result["answer"] == "Ada Lovelace wrote it."  # caller-supplied answer evaluated as-is
    # retrieved contexts mirror the retrieval envelope
    contexts = result["retrieved_contexts"]
    assert len(contexts) == 4
    assert contexts[0]["node_id"] == "4:x:1"
    assert contexts[0]["node_labels"] == ["Chunk"]
    assert contexts[0]["relevance_score"] == 0.03
    assert "ada lovelace" in contexts[0]["content"]


async def test_answer_relevance_clamped_into_unit_interval() -> None:
    high = FakeJudge(_happy_responses(**{ev.RELEVANCE_SYSTEM: '{"score": 3}'}))
    result = await _evaluate(_service(high), metrics=["answer_relevance"])
    assert result["scores"]["answer_relevance"] == 1.0
    low = FakeJudge(_happy_responses(**{ev.RELEVANCE_SYSTEM: '{"score": -0.5}'}))
    result = await _evaluate(_service(low), metrics=["answer_relevance"])
    assert result["scores"]["answer_relevance"] == 0.0


async def test_context_precision_fraction_per_chunk() -> None:
    # only the chunk mentioning ada is relevant → 1 of 4 → 0.25
    judge = FakeJudge(
        _happy_responses(
            **{
                ev.PRECISION_SYSTEM: lambda user: (
                    '{"relevant": true}' if "ada" in user else '{"relevant": false}'
                )
            }
        )
    )
    result = await _evaluate(_service(judge), metrics=["context_precision"])
    assert result["scores"]["context_precision"] == 0.25
    assert len(judge.calls_for(ev.PRECISION_SYSTEM)) == 4


async def test_context_recall_fraction_of_statements() -> None:
    judge = FakeJudge(
        _happy_responses(
            **{
                ev.RECALL_VERDICT_SYSTEM: lambda user: (
                    '{"attributable": true}' if "fact one" in user else '{"attributable": false}'
                )
            }
        )
    )
    result = await _evaluate(_service(judge), ground_truth="Two facts.", metrics=["context_recall"])
    assert result["scores"]["context_recall"] == 0.5


async def test_scores_round_to_four_decimal_places() -> None:
    # 1 of 3 claims supported → 1/3 → 0.3333
    judge = FakeJudge(
        _happy_responses(
            **{
                ev.CLAIM_VERDICT_SYSTEM: lambda user: (
                    '{"supported": true}' if "claim one" in user else '{"supported": false}'
                )
            }
        )
    )
    result = await _evaluate(_service(judge), metrics=["faithfulness"])
    assert result["scores"]["faithfulness"] == 0.3333


# --- metric subset selection + ground_truth gating ----------------------------------------------


async def test_metric_subset_runs_only_requested_judging() -> None:
    judge = FakeJudge(_happy_responses())
    result = await _evaluate(_service(judge), metrics=["context_precision"])
    assert result["metrics_computed"] == ["context_precision"]
    assert result["scores"]["faithfulness"] is None
    assert result["scores"]["answer_relevance"] is None
    assert judge.calls_for(ev.CLAIMS_SYSTEM) == []
    assert judge.calls_for(ev.RELEVANCE_SYSTEM) == []
    assert judge.text_calls == []  # no answer metric requested → no generation either


async def test_unknown_metric_warned_and_ignored() -> None:
    judge = FakeJudge(_happy_responses())
    result = await _evaluate(_service(judge), metrics=["context_precision", "bogus"])
    assert result["metrics_computed"] == ["context_precision"]
    assert any("Unknown metrics ignored: ['bogus']" in w for w in result["warnings"])


async def test_all_unknown_metrics_is_a_caller_error() -> None:
    with pytest.raises(NoValidMetrics):
        await _evaluate(_service(FakeJudge(_happy_responses())), metrics=["bogus"])


async def test_explicit_empty_metrics_list_is_a_caller_error() -> None:
    # metrics: [] is an is-None check, not truthiness — an explicit empty list means the caller
    # asked for nothing computable (#333), never "give me the defaults".
    with pytest.raises(NoValidMetrics):
        await _evaluate(_service(FakeJudge(_happy_responses())), metrics=[])


async def test_context_recall_gated_on_ground_truth() -> None:
    # default metric set, no ground_truth → recall skipped with a warning, others computed
    judge = FakeJudge(_happy_responses())
    result = await _evaluate(_service(judge), ground_truth=None)
    assert result["scores"]["context_recall"] is None
    assert "context_recall" not in result["metrics_computed"]
    assert any("context_recall skipped: ground_truth not provided" in w for w in result["warnings"])
    # explicitly requesting ONLY recall without ground_truth leaves nothing to compute → 422 path
    with pytest.raises(NoValidMetrics):
        await _evaluate(_service(judge), ground_truth=None, metrics=["context_recall"])


# --- fail-soft matrix ----------------------------------------------------------------------------


async def test_judge_raises_nulls_that_metric_only() -> None:
    def _boom(user: str) -> str:
        raise RuntimeError("provider down")

    judge = FakeJudge(_happy_responses(**{ev.CLAIM_VERDICT_SYSTEM: _boom}))
    result = await _evaluate(_service(judge), ground_truth="Ada did.")
    assert result["scores"]["faithfulness"] is None
    # EVERY verdict failed → strict majority → the metric nulls with the batch warnings (#333)
    assert any("faithfulness: 3 of 3 verdict calls failed." in w for w in result["warnings"])
    assert any(
        "faithfulness skipped: a majority of verdict calls failed." in w for w in result["warnings"]
    )
    # the other metrics still computed — one failing judge call never sinks the evaluation
    assert result["scores"]["answer_relevance"] == 0.9
    assert result["scores"]["context_precision"] == 1.0
    assert result["scores"]["context_recall"] == 1.0
    assert result["metrics_computed"] == ["answer_relevance", "context_precision", "context_recall"]
    assert result["overall"] == round((0.9 + 1.0 + 1.0) / 3, 4)
    assert result["is_grounded"] is False  # faithfulness not computed → never grounded


async def test_malformed_judge_json_nulls_that_metric_only() -> None:
    judge = FakeJudge(
        _happy_responses(
            **{
                ev.CLAIMS_SYSTEM: "not json at all",
                ev.RELEVANCE_SYSTEM: '{"score": "very relevant"}',
            }
        )
    )
    result = await _evaluate(_service(judge))
    assert result["scores"]["faithfulness"] is None
    assert result["scores"]["answer_relevance"] is None
    assert result["scores"]["context_precision"] == 1.0
    malformed = [w for w in result["warnings"] if "malformed response" in w]
    assert len(malformed) == 2
    assert any(w.startswith("faithfulness") for w in malformed)
    assert any(w.startswith("answer_relevance") for w in malformed)


async def test_no_claims_extracted_is_null_with_warning() -> None:
    judge = FakeJudge(_happy_responses(**{ev.CLAIMS_SYSTEM: '{"claims": []}'}))
    result = await _evaluate(_service(judge), metrics=["faithfulness", "context_precision"])
    assert result["scores"]["faithfulness"] is None
    assert any("no factual claims" in w for w in result["warnings"])
    assert result["scores"]["context_precision"] == 1.0


async def test_non_finite_score_is_rejected_not_clamped() -> None:
    # NaN/Infinity must never be clamp-fabricated into a real score (#333): null + warning.
    for raw in ('{"score": NaN}', '{"score": Infinity}', '{"score": -Infinity}'):
        judge = FakeJudge(_happy_responses(**{ev.RELEVANCE_SYSTEM: raw}))
        result = await _evaluate(_service(judge), metrics=["answer_relevance"])
        assert result["scores"]["answer_relevance"] is None, raw
        assert any(
            "answer_relevance skipped: the judge returned a malformed response." in w
            for w in result["warnings"]
        ), raw


# --- partial verdict-batch failures (#333: fraction over successes, majority-fail null) ----------


async def test_minority_verdict_failure_scores_over_successes_with_warning() -> None:
    # 1 of 3 claim verdicts fails; the 2 successes split true/false → 0.5 over SUCCESSES,
    # proving the denominator excludes the failed call. Siblings all complete (no early bail).
    def _verdict(user: str) -> str:
        if "claim one" in user:
            return '{"supported": true}'
        if "claim two" in user:
            return '{"supported": false}'
        raise RuntimeError("provider blip")

    judge = FakeJudge(_happy_responses(**{ev.CLAIM_VERDICT_SYSTEM: _verdict}))
    result = await _evaluate(_service(judge), metrics=["faithfulness"])
    assert result["scores"]["faithfulness"] == 0.5
    assert result["metrics_computed"] == ["faithfulness"]
    assert any("faithfulness: 1 of 3 verdict calls failed." in w for w in result["warnings"])
    assert len(judge.calls_for(ev.CLAIM_VERDICT_SYSTEM)) == 3  # every sibling still ran


async def test_majority_verdict_failures_null_the_metric() -> None:
    # 2 of 3 fail — a STRICT MAJORITY (failures × 2 > total, the documented threshold) → null.
    def _verdict(user: str) -> str:
        if "claim one" in user:
            return '{"supported": true}'
        raise RuntimeError("provider down")

    judge = FakeJudge(_happy_responses(**{ev.CLAIM_VERDICT_SYSTEM: _verdict}))
    result = await _evaluate(_service(judge), metrics=["faithfulness"])
    assert result["scores"]["faithfulness"] is None
    assert any("faithfulness: 2 of 3 verdict calls failed." in w for w in result["warnings"])
    assert any(
        "faithfulness skipped: a majority of verdict calls failed." in w for w in result["warnings"]
    )


# --- the overall deadline (#333: partial results, in-flight judge calls genuinely cancelled) -----


async def test_deadline_returns_partial_result_and_cancels_inflight_judge_calls() -> None:
    # faithfulness's claim verdicts HANG; answer_relevance / context_precision complete fast.
    # On expiry: the completed metrics are returned, the hung calls are CANCELLED (pure-async
    # judge calls — cancellation propagates, unlike the #327 to_thread case), never a 504.
    hang_started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _hang() -> str:
        hang_started.set()
        try:
            await asyncio.Event().wait()  # hangs forever unless cancelled
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return '{"supported": true}'

    judge = FakeJudge(_happy_responses(**{ev.CLAIM_VERDICT_SYSTEM: lambda user: _hang()}))
    service = _service(judge, deadline_seconds=0.25)
    result = await asyncio.wait_for(_evaluate(service), timeout=5.0)  # returns promptly
    assert hang_started.is_set()
    assert cancelled.is_set(), "the deadline must actually stop in-flight judge calls"
    assert result["scores"]["faithfulness"] is None
    assert result["scores"]["answer_relevance"] == 0.9
    assert result["scores"]["context_precision"] == 1.0
    assert result["metrics_computed"] == ["answer_relevance", "context_precision"]
    assert any(
        "faithfulness skipped: evaluation deadline" in w and "partial results returned" in w
        for w in result["warnings"]
    )


# --- spend caps (#333: per-request concurrency + process-level capacity) -------------------------


async def test_peak_inflight_judge_calls_bounded_by_max_concurrency() -> None:
    # ONE counting fake judge across ALL metrics: peak concurrent judge calls ≤ the request cap.
    inflight = 0
    peak = 0

    async def _counted(response: str) -> str:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.005)
        inflight -= 1
        return response

    responses = {
        system: (lambda user, _r=resp: _counted(_r)) for system, resp in _happy_responses().items()
    }
    judge = FakeJudge(responses)
    result = await _evaluate(
        _service(judge, max_concurrency=2), ground_truth="Ada Lovelace wrote it first."
    )
    assert result["metrics_computed"] == [
        "faithfulness",
        "answer_relevance",
        "context_precision",
        "context_recall",
    ]
    assert len(judge.json_calls) >= 8  # the bound was actually exercised across metrics
    assert peak <= 2


async def test_capacity_exhausted_raises_typed_error() -> None:
    # process-level slots taken → a typed EvaluationCapacityExceeded (the route maps it to 429)
    slots = asyncio.Semaphore(1)
    await slots.acquire()  # another evaluation holds the only slot
    service = _service(FakeJudge(_happy_responses()), request_slots=slots, slot_wait_seconds=0.05)
    with pytest.raises(EvaluationCapacityExceeded):
        await _evaluate(service)


async def test_capacity_slot_is_released_after_an_evaluation() -> None:
    slots = asyncio.Semaphore(1)
    service = _service(FakeJudge(_happy_responses()), request_slots=slots, slot_wait_seconds=0.05)
    await _evaluate(service)
    result = await _evaluate(service)  # the slot came back — a second run acquires it
    assert result["metrics_computed"]


# --- the answer-generation branch ----------------------------------------------------------------


async def test_missing_answer_is_generated_from_retrieved_context() -> None:
    judge = FakeJudge(_happy_responses())
    result = await _evaluate(
        _service(judge), answer=None, metrics=["faithfulness", "answer_relevance"]
    )
    assert result["answer"] == _GENERATED
    assert len(judge.text_calls) == 1
    system, user = judge.text_calls[0]
    assert system == ev.ANSWER_SYSTEM
    assert "ada lovelace wrote the first program" in user  # grounded on the retrieved context
    assert "Who wrote the first computer program?" in user
    assert result["scores"]["faithfulness"] == 1.0  # the generated answer was what got judged


async def test_answer_generation_failure_skips_answer_metrics_failsoft() -> None:
    judge = FakeJudge(_happy_responses(), text_response=RuntimeError("provider down"))
    result = await _evaluate(_service(judge), answer=None, ground_truth="Ada did.")
    assert result["answer"] is None
    assert result["scores"]["faithfulness"] is None
    assert result["scores"]["answer_relevance"] is None
    assert any("faithfulness skipped: answer generation failed." in w for w in result["warnings"])
    assert any(
        "answer_relevance skipped: answer generation failed." in w for w in result["warnings"]
    )
    # the answer-independent metrics still run
    assert result["scores"]["context_precision"] == 1.0
    assert result["scores"]["context_recall"] == 1.0


# --- cost caps / bounds ---------------------------------------------------------------------------


async def test_claims_cap_truncates_and_warns() -> None:
    judge = FakeJudge(
        _happy_responses(**{ev.CLAIMS_SYSTEM: '{"claims": ["c1", "c2", "c3", "c4"]}'})
    )
    result = await _evaluate(_service(judge, max_claims=2), metrics=["faithfulness"])
    assert len(judge.calls_for(ev.CLAIM_VERDICT_SYSTEM)) == 2
    assert any("judged the first 2 of 4 claims" in w for w in result["warnings"])
    assert result["scores"]["faithfulness"] == 1.0


async def test_context_judging_capped_at_max_contexts() -> None:
    judge = FakeJudge(_happy_responses())
    result = await _evaluate(_service(judge, max_contexts=2), metrics=["context_precision"])
    assert len(judge.calls_for(ev.PRECISION_SYSTEM)) == 2
    assert result["scores"]["context_precision"] == 1.0


async def test_context_cap_applies_once_so_every_metric_judges_the_same_set() -> None:
    # max_contexts=2 over 4 retrieved (#333): the slice happens ONCE up front — precision,
    # faithfulness, recall AND answer generation all see chunks 1–2 only; warned; the response's
    # retrieved_contexts is that same judged set.
    judge = FakeJudge(_happy_responses())
    result = await _evaluate(_service(judge, max_contexts=2), answer=None, ground_truth="Ada did.")
    assert any(
        "Judging the first 2 of 4 retrieved contexts (cap)." in w for w in result["warnings"]
    )
    assert len(result["retrieved_contexts"]) == 2
    assert len(judge.calls_for(ev.PRECISION_SYSTEM)) == 2
    context_users = (
        judge.calls_for(ev.CLAIM_VERDICT_SYSTEM)
        + judge.calls_for(ev.RECALL_VERDICT_SYSTEM)
        + [user for _, user in judge.text_calls]  # the generated answer's grounding prompt
    )
    assert context_users
    for user in context_users:
        assert "ada lovelace wrote the first program" in user  # chunk 1 (kept)
        assert "the engine was mechanical" not in user  # chunk 3 (dropped)
        assert "grace hopper built a compiler" not in user  # chunk 4 (dropped)


async def test_decomposition_prompts_carry_the_claims_cap() -> None:
    # the cap is pushed INTO the decomposition prompts (#333) — the post-parse slice is backstop
    judge = FakeJudge(_happy_responses())
    await _evaluate(
        _service(judge, max_claims=7),
        ground_truth="Ada did.",
        metrics=["faithfulness", "context_recall"],
    )
    (claims_user,) = judge.calls_for(ev.CLAIMS_SYSTEM)
    assert "Return at most 7 claims." in claims_user
    (statements_user,) = judge.calls_for(ev.STATEMENTS_SYSTEM)
    assert "Return at most 7 statements." in statements_user


# --- graph scoping + empty retrieval --------------------------------------------------------------


async def test_unknown_or_cross_org_graph_raises_not_found() -> None:
    service = _service(FakeJudge(_happy_responses()), retrieval=FakeRetrieval(exists=False))
    with pytest.raises(GraphNotFound):
        await _evaluate(service)


async def test_empty_retrieval_warns_and_judges_against_placeholder() -> None:
    judge = FakeJudge(_happy_responses(**{ev.PRECISION_SYSTEM: '{"relevant": false}'}))
    service = _service(judge, retrieval=FakeRetrieval(nodes=[]))
    result = await _evaluate(service, metrics=["context_precision"])
    assert any("No context retrieved" in w for w in result["warnings"])
    assert result["retrieved_contexts"] == []
    assert result["scores"]["context_precision"] == 0.0  # the placeholder is honestly irrelevant


# --- the judge factory (no key → None → DI maps to a typed 422) -----------------------------------


def test_make_judge_without_key_returns_none() -> None:
    assert make_judge(Settings(openai_api_key=None)) is None


def test_make_judge_with_key_builds_the_openai_client() -> None:
    judge = make_judge(Settings(openai_api_key="test-key"))
    assert isinstance(judge, OpenAIEvalJudge)
    assert isinstance(judge, EvalJudge)  # satisfies the protocol seam


def test_make_judge_applies_the_timeout_and_retry_posture() -> None:
    # #333: NEVER the SDK defaults (600s × 3 attempts) — explicit short timeout, bounded retries.
    judge = make_judge(
        Settings(
            openai_api_key="test-key",
            eval_judge_timeout_seconds=9.0,
            eval_judge_max_retries=2,
        )
    )
    assert judge is not None
    assert judge._client.timeout == 9.0
    assert judge._client.max_retries == 2
    # and the config default itself is short — under the gateway's 30s read timeout
    defaults = Settings(openai_api_key="test-key")
    assert defaults.eval_judge_timeout_seconds <= 15.0
    assert defaults.eval_judge_max_retries <= 1
    assert defaults.eval_deadline_seconds < 30.0


# --- the lifespan judge singleton (#333: one client per process, closed on shutdown) -------------


async def test_lifespan_builds_one_judge_and_the_eval_slots(monkeypatch) -> None:
    from oraclous_knowledge_retriever_service.app.factory import create_app
    from oraclous_knowledge_retriever_service.core import lifespan as lifespan_module

    monkeypatch.setenv("KRS_OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("KRS_NEO4J_URI", raising=False)
    monkeypatch.delenv("EXIT_ON_STARTUP_DEGRADE", raising=False)
    get_settings.cache_clear()
    closed: list[bool] = []
    try:
        app = create_app()
        async with lifespan_module.lifespan(app):
            judge = app.state.eval_judge
            assert isinstance(judge, OpenAIEvalJudge)  # built ONCE at startup, not per request
            assert isinstance(app.state.eval_slots, asyncio.Semaphore)
            original_aclose = judge.aclose

            async def _spy() -> None:
                closed.append(True)
                await original_aclose()

            monkeypatch.setattr(judge, "aclose", _spy)
        assert closed == [True]  # the client is closed on shutdown
    finally:
        get_settings.cache_clear()


async def test_lifespan_without_a_key_leaves_the_judge_unconfigured(monkeypatch) -> None:
    from oraclous_knowledge_retriever_service.app.factory import create_app
    from oraclous_knowledge_retriever_service.core import lifespan as lifespan_module

    monkeypatch.delenv("KRS_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KRS_NEO4J_URI", raising=False)
    monkeypatch.delenv("EXIT_ON_STARTUP_DEGRADE", raising=False)
    get_settings.cache_clear()
    try:
        app = create_app()
        async with lifespan_module.lifespan(app):
            assert app.state.eval_judge is None  # DI maps this to the typed 422
    finally:
        get_settings.cache_clear()
