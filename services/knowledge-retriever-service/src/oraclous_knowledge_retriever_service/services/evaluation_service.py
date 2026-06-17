"""RAGAS-style retrieval-quality evaluation (ORAA-4 §21 services layer) (#331).

NATIVE LLM-judge implementations of the four RAGAS metrics — direct prompts against the one
OpenAI-compatible judge client, NOT the ragas/langchain/pandas stack the legacy
``evaluation_service.py`` pulled in. The metric SEMANTICS are lifted from the legacy service:

  faithfulness       — decompose the answer into atomic claims (one judge call), then judge each
                       claim supported-by-context (concurrent) → supported / total. Matches RAGAS
                       semantics.
  answer_relevance   — judge how directly the answer addresses the question → 0–1. This is a
                       DIRECT judge score, NOT RAGAS's generated-questions + cosine-similarity
                       procedure — simpler, but a different estimator of the same intent.
  context_precision  — judge each retrieved chunk's relevance to the question → relevant / total.
                       This is an order-INSENSITIVE fraction; RAGAS's original context_precision
                       is rank-weighted (mean precision@k over the relevant positions).
  context_recall     — (only with ground_truth) decompose the ground truth into statements, judge
                       each attributable to the retrieved context → found / total. Matches RAGAS
                       semantics.

Retrieval goes through the EXISTING KRS read path (hybrid, top_k≈5); the judged context set is
capped ONCE up front (``eval_max_contexts``, warned when it drops chunks) so precision,
faithfulness, recall AND answer generation all judge the SAME contexts. When ``answer`` is absent
and an answer-dependent metric is requested, one is GENERATED from the retrieved context
(retrieve → grounded-answer prompt → the judge LLM) so the endpoint evaluates retrieval+generation
end-to-end. NOTE the self-judging bias on that path: the same model writes AND grades the answer,
so faithfulness/answer_relevance skew optimistic there.

Fail-soft per metric: a judge failure or malformed judge response nulls THAT metric and appends a
warning — never a 500. Within a verdict batch, one failed call no longer sinks the metric: the
batch gathers with ``return_exceptions=True``, the fraction is computed over the verdicts that
SUCCEEDED (warned "N of M verdict calls failed"), and the metric nulls only when a STRICT MAJORITY
of its verdict calls fail (threshold: failures × 2 > total).

Spend bounds: judge calls are concurrency-limited by one ``asyncio.Semaphore`` per request (the
KGS #272 pattern); evaluations themselves are capped process-wide (``eval_max_concurrent_requests``
slots, built at lifespan — excess queues briefly then gets a typed 429); claims/statements are
capped both IN the decomposition prompt and by a post-parse slice; and the WHOLE evaluation runs
under ``eval_deadline_seconds`` (default 25s — under the gateway's 30s read timeout). On deadline
expiry the response carries every metric that completed, with nulls + warnings for the rest —
partial results, never a 504-then-burn. The judge calls are pure async (AsyncOpenAI), so the
deadline genuinely CANCELS them in flight (the #327 ``to_thread`` caveat applies only to the
local, fast Neo4j reads).

Scores are 0–1 rounded to 4 dp; ``overall`` is the mean of the computed scores; ``is_grounded`` is
faithfulness ≥ a config threshold. Evaluation issues no Neo4j writes; the inherited retrieval path
may write advisory Redis query-cache entries when ``KRS_QUERY_CACHE=true``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Coroutine, Iterable

from oraclous_knowledge_retriever_service.services.eval_judge import EvalJudge
from oraclous_knowledge_retriever_service.services.retrieval_service import RetrievalService

logger = logging.getLogger(__name__)

SUPPORTED_METRICS = frozenset(
    {"faithfulness", "answer_relevance", "context_precision", "context_recall"}
)

# Canonical metric order — drives deterministic metrics_computed / warning ordering.
_METRIC_ORDER = ("faithfulness", "answer_relevance", "context_precision", "context_recall")

# Metrics that judge the answer text (need one — caller-supplied or generated).
_ANSWER_METRICS = frozenset({"faithfulness", "answer_relevance"})

# Per-chunk character cap inside judge prompts (bounds prompt spend; chunks are typically ≤2k).
_CONTEXT_CHAR_CAP = 4000

# The legacy placeholder when retrieval finds nothing — keeps judge prompts well-formed and yields
# honest near-zero scores instead of an error.
_NO_CONTEXT_PLACEHOLDER = "No relevant context found in the knowledge graph."

# --- judge system prompts (module constants: stable contracts; tests dispatch fakes on them) ---

ANSWER_SYSTEM = (
    "You answer questions strictly from the provided knowledge-graph context. "
    "If the context does not contain the answer, say you do not know. Be concise."
)

CLAIMS_SYSTEM = (
    "You decompose an answer into atomic factual claims. Each claim must be a single, "
    "self-contained statement taken from the answer — never add facts that are not in the "
    'answer. Respond ONLY with a JSON object: {"claims": ["...", "..."]}.'
)

CLAIM_VERDICT_SYSTEM = (
    "You judge whether a claim is supported by the provided context. The claim is supported "
    "only if the context states or directly implies it. Respond ONLY with a JSON object: "
    '{"supported": true} or {"supported": false}.'
)

RELEVANCE_SYSTEM = (
    "You judge how directly an answer addresses a question, ignoring whether it is factually "
    "correct. 1.0 = fully and directly addresses it; 0.0 = does not address it at all. "
    "Evasive, noncommittal or off-topic answers score low. Respond ONLY with a JSON object: "
    '{"score": <number between 0 and 1>}.'
)

PRECISION_SYSTEM = (
    "You judge whether a retrieved context chunk is relevant to answering a question. "
    'Respond ONLY with a JSON object: {"relevant": true} or {"relevant": false}.'
)

STATEMENTS_SYSTEM = (
    "You decompose a reference answer into atomic factual statements. Each statement must be "
    "a single, self-contained fact from the reference — never add facts that are not in it. "
    'Respond ONLY with a JSON object: {"statements": ["...", "..."]}.'
)

RECALL_VERDICT_SYSTEM = (
    "You judge whether a statement can be attributed to the provided context — i.e. the "
    "context contains the information needed to derive it. Respond ONLY with a JSON object: "
    '{"attributable": true} or {"attributable": false}.'
)


class GraphNotFound(Exception):
    """The bound organisation has no graph with this id (also covers other orgs' graphs → 404)."""


class NoValidMetrics(ValueError):
    """The request left no computable metric (e.g. only unknown names) — a caller error (422)."""


class EvaluationCapacityExceeded(Exception):
    """Too many evaluations in flight process-wide — a typed 429 (judge-spend protection, #333)."""


class JudgeResponseError(Exception):
    """The judge returned output a metric step could not parse (→ that metric nulls, fail-soft)."""


# --- judge-response parsing (strict: a malformed response fails THAT metric, never fakes a score) -


def _parse_json_object(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise JudgeResponseError("judge returned non-JSON output") from exc
    if not isinstance(data, dict):
        raise JudgeResponseError("judge returned a non-object JSON payload")
    return data


def _parse_string_list(raw: str, key: str) -> list[str]:
    items = _parse_json_object(raw).get(key)
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise JudgeResponseError(f"judge response missing a string list under {key!r}")
    return [i.strip() for i in items if i.strip()]


def _parse_bool(raw: str, key: str) -> bool:
    value = _parse_json_object(raw).get(key)
    if not isinstance(value, bool):
        raise JudgeResponseError(f"judge response missing a boolean under {key!r}")
    return value


def _parse_score(raw: str) -> float:
    value = _parse_json_object(raw).get("score")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise JudgeResponseError("judge response missing a numeric 'score'")
    if not math.isfinite(value):
        # never clamp-fabricate from NaN/Infinity — that metric nulls with a warning
        raise JudgeResponseError("judge returned a non-finite 'score'")
    return min(1.0, max(0.0, float(value)))


def _context_block(contexts: list[str]) -> str:
    return "\n\n".join(f"[{i + 1}] {text}" for i, text in enumerate(contexts))


class _EvalState:
    """Mutable per-evaluation state the deadline handler reads after cancellation (#333).

    Judge calls are pure async, so deadline cancellation is clean: everything recorded here
    before expiry is complete and correct, and the partial-result response is built from it.
    """

    def __init__(self, *, requested: set[str], answer: str | None) -> None:
        self.requested = requested
        self.answer = answer
        self.context_items: list[dict] = []
        self.scores: dict[str, float | None] = dict.fromkeys(_METRIC_ORDER)
        self.completed: set[str] = set()  # metrics that finished (incl. fail-soft nulls)
        self.buckets: dict[str, list[str]] = {}  # per-metric warning buffers


class EvaluationService:
    """Evaluate one question (and optionally an answer / ground truth) against a graph."""

    def __init__(
        self,
        *,
        retrieval: RetrievalService,
        judge: EvalJudge,
        top_k: int = 5,
        max_concurrency: int = 5,
        max_claims: int = 25,
        max_contexts: int = 5,
        grounded_threshold: float = 0.7,
        deadline_seconds: float = 25.0,
        request_slots: asyncio.Semaphore | None = None,
        slot_wait_seconds: float = 1.0,
    ) -> None:
        self._retrieval = retrieval
        self._judge = judge
        self._top_k = top_k
        self._max_concurrency = max(1, max_concurrency)
        self._max_claims = max(1, max_claims)
        self._max_contexts = max(1, max_contexts)
        self._grounded_threshold = grounded_threshold
        self._deadline_seconds = deadline_seconds
        # Process-level evaluation slots (lifespan-built, shared across requests); None → uncapped.
        self._request_slots = request_slots
        self._slot_wait_seconds = slot_wait_seconds

    # ------------------------------------------------------------------ public

    async def evaluate(
        self,
        *,
        graph_id: str,
        question: str,
        answer: str | None,
        ground_truth: str | None,
        metrics: list[str] | None,
    ) -> dict:
        """Run the requested metrics and return the evaluation result dict.

        Raises :class:`GraphNotFound` (→ 404), :class:`NoValidMetrics` (→ 422) and
        :class:`EvaluationCapacityExceeded` (→ 429); every judge failure inside a metric is
        fail-soft (that metric → None + a warning). The whole flow runs under the configured
        deadline: on expiry, in-flight judge calls are cancelled and the metrics that completed
        are returned with nulls + warnings for the rest — partial results, never an error.
        """
        warnings: list[str] = []
        requested = self._resolve_metrics(metrics, ground_truth, warnings)

        await self._acquire_slot()
        try:
            state = _EvalState(requested=requested, answer=answer)
            try:
                async with asyncio.timeout(self._deadline_seconds):
                    await self._evaluate_bounded(
                        state=state,
                        graph_id=graph_id,
                        question=question,
                        ground_truth=ground_truth,
                        warnings=warnings,
                    )
            except TimeoutError:
                for name in (n for n in _METRIC_ORDER if n in state.requested - state.completed):
                    warnings.append(
                        f"{name} skipped: evaluation deadline ({self._deadline_seconds:g}s) "
                        "exceeded; partial results returned."
                    )
            return self._build_result(state, warnings)
        finally:
            self._release_slot()

    # ------------------------------------------------------------------ internal

    async def _acquire_slot(self) -> None:
        """Take a process-level evaluation slot: queue briefly, then a typed 429 (#333)."""
        if self._request_slots is None:
            return
        try:
            await asyncio.wait_for(self._request_slots.acquire(), timeout=self._slot_wait_seconds)
        except TimeoutError:
            raise EvaluationCapacityExceeded(
                "too many evaluations in flight; retry shortly"
            ) from None

    def _release_slot(self) -> None:
        if self._request_slots is not None:
            self._request_slots.release()

    @staticmethod
    def _resolve_metrics(
        metrics: list[str] | None, ground_truth: str | None, warnings: list[str]
    ) -> set[str]:
        """Resolve the metric subset (legacy semantics): unknown names warn + drop;
        context_recall is gated on ground_truth; nothing left → NoValidMetrics (422).
        An EXPLICIT empty list is a caller error too (is-None check, not truthiness)."""
        requested = set(metrics) if metrics is not None else set(SUPPORTED_METRICS)
        unknown = requested - SUPPORTED_METRICS
        if unknown:
            warnings.append(f"Unknown metrics ignored: {sorted(unknown)}")
            requested -= unknown
        if not ground_truth and "context_recall" in requested:
            warnings.append("context_recall skipped: ground_truth not provided.")
            requested.discard("context_recall")
        if not requested:
            raise NoValidMetrics("No valid metrics to compute.")
        return requested

    async def _evaluate_bounded(
        self,
        *,
        state: _EvalState,
        graph_id: str,
        question: str,
        ground_truth: str | None,
        warnings: list[str],
    ) -> None:
        """The deadline-scoped flow: probe → retrieve → (generate answer) → run metrics."""
        if not await self._retrieval.graph_exists(graph_id=graph_id):
            raise GraphNotFound(graph_id)

        context_strings = await self._retrieve(
            state, graph_id=graph_id, query=question, warnings=warnings
        )

        semaphore = asyncio.Semaphore(self._max_concurrency)

        # Answer: caller-supplied, or generated from the retrieved context when an
        # answer-dependent metric needs one (retrieve → grounded-answer prompt → the judge LLM).
        if state.answer is None and state.requested & _ANSWER_METRICS:
            state.answer = await self._generate_answer(
                question=question, contexts=context_strings, semaphore=semaphore
            )
            if state.answer is None:
                for name in sorted(state.requested & _ANSWER_METRICS):
                    warnings.append(f"{name} skipped: answer generation failed.")
                state.requested -= _ANSWER_METRICS

        await self._run_metrics(
            state=state,
            question=question,
            ground_truth=ground_truth,
            contexts=context_strings,
            semaphore=semaphore,
        )

    def _build_result(self, state: _EvalState, warnings: list[str]) -> dict:
        """Assemble the response dict — shared by the complete and deadline-partial paths."""
        for name in _METRIC_ORDER:
            warnings.extend(state.buckets.get(name, []))
        computed = [name for name in _METRIC_ORDER if state.scores[name] is not None]
        values = [v for name in computed if (v := state.scores[name]) is not None]
        overall = round(sum(values) / len(values), 4) if values else None
        faithfulness = state.scores["faithfulness"]
        is_grounded = faithfulness is not None and faithfulness >= self._grounded_threshold
        return {
            "answer": state.answer,
            "retrieved_contexts": state.context_items,
            "scores": state.scores,
            "overall": overall,
            "metrics_computed": computed,
            "is_grounded": is_grounded,
            "warnings": warnings,
        }

    async def _retrieve(
        self, state: _EvalState, *, graph_id: str, query: str, warnings: list[str]
    ) -> list[str]:
        """Fetch the judged context set via the EXISTING KRS hybrid read path.

        The ``max_contexts`` cap is applied ONCE here (warned when it drops chunks) so precision,
        faithfulness, recall and answer generation all judge the SAME context set (#333).
        """
        nodes = await self._retrieval.hybrid(graph_id=graph_id, query=query, top_k=self._top_k)
        if len(nodes) > self._max_contexts:
            warnings.append(
                f"Judging the first {self._max_contexts} of {len(nodes)} retrieved contexts (cap)."
            )
            nodes = nodes[: self._max_contexts]
        items: list[dict] = []
        strings: list[str] = []
        for node in nodes:
            props = node["properties"]
            text = str(props.get("text") or "")
            relevance = props.get("rrf_score", props.get("score"))
            items.append(
                {
                    "node_id": node["id"],
                    "node_labels": [node["type"]],
                    "content": text,
                    "relevance_score": (
                        float(relevance)
                        if isinstance(relevance, int | float) and not isinstance(relevance, bool)
                        else None
                    ),
                }
            )
            if text:
                strings.append(text[:_CONTEXT_CHAR_CAP])
        state.context_items = items
        if not strings:
            warnings.append(
                "No context retrieved from the graph; scores judge against an empty context."
            )
            strings = [_NO_CONTEXT_PLACEHOLDER]
        return strings

    async def _judge_json(self, semaphore: asyncio.Semaphore, *, system: str, user: str) -> str:
        async with semaphore:
            return await self._judge.complete_json(system=system, user=user)

    async def _generate_answer(
        self, *, question: str, contexts: list[str], semaphore: asyncio.Semaphore
    ) -> str | None:
        """Generate a grounded answer from the retrieved context, or None on failure (fail-soft:
        the answer-dependent metrics are then skipped with warnings — never a 500)."""
        user = f"Context:\n{_context_block(contexts)}\n\nQuestion: {question}\n\nAnswer:"
        try:
            async with semaphore:
                text = await self._judge.complete_text(system=ANSWER_SYSTEM, user=user)
        except Exception as exc:  # noqa: BLE001 — fail-soft: skip answer metrics, never 500
            logger.warning("evaluation: answer generation failed: %r", exc)
            return None
        text = (text or "").strip()
        return text or None

    async def _run_metrics(
        self,
        *,
        state: _EvalState,
        question: str,
        ground_truth: str | None,
        contexts: list[str],
        semaphore: asyncio.Semaphore,
    ) -> None:
        """Run the requested metrics concurrently; each is individually fail-soft.

        Each metric writes its score into ``state`` the moment it finishes, so a deadline
        cancellation mid-flight still leaves every completed metric in the response. Warnings are
        buffered per metric and merged in canonical order (``_build_result``) so the response is
        deterministic regardless of completion order.
        """
        ordered = [name for name in _METRIC_ORDER if name in state.requested]
        for name in ordered:
            state.buckets[name] = []

        async def _run(name: str) -> None:
            bucket = state.buckets[name]
            value: float | None = None
            try:
                if name == "faithfulness":
                    value = await self._faithfulness(
                        question=question,
                        answer=state.answer or "",
                        contexts=contexts,
                        semaphore=semaphore,
                        warnings=bucket,
                    )
                elif name == "answer_relevance":
                    value = await self._answer_relevance(
                        question=question, answer=state.answer or "", semaphore=semaphore
                    )
                elif name == "context_precision":
                    value = await self._context_precision(
                        question=question, contexts=contexts, semaphore=semaphore, warnings=bucket
                    )
                else:
                    value = await self._context_recall(
                        ground_truth=ground_truth or "",
                        contexts=contexts,
                        semaphore=semaphore,
                        warnings=bucket,
                    )
            except JudgeResponseError as exc:
                bucket.append(f"{name} skipped: the judge returned a malformed response.")
                logger.warning("evaluation: %s got a malformed judge response: %s", name, exc)
            except Exception as exc:  # noqa: BLE001 — fail-soft per metric, never a 500
                bucket.append(f"{name} skipped: the judge call failed.")
                logger.warning("evaluation: %s judge call failed: %r", name, exc)
            state.scores[name] = round(value, 4) if value is not None else None
            state.completed.add(name)

        await asyncio.gather(*(_run(name) for name in ordered))

    async def _verdict_fraction(
        self,
        *,
        metric: str,
        calls: Iterable[Coroutine[object, object, bool]],
        warnings: list[str],
    ) -> float | None:
        """Fraction of true verdicts over the calls that SUCCEEDED (#333).

        ``return_exceptions=True`` keeps one failed verdict from sinking the batch — and from
        propagating early while sibling calls keep spending unobserved. The metric nulls only
        when a STRICT MAJORITY of its verdict calls fail (failures × 2 > total); below that the
        fraction over the successful verdicts is still meaningful signal, surfaced with a warning.
        """
        results = await asyncio.gather(*calls, return_exceptions=True)
        successes = [r for r in results if isinstance(r, bool)]
        failed = len(results) - len(successes)
        if failed:
            warnings.append(f"{metric}: {failed} of {len(results)} verdict calls failed.")
            logger.warning(
                "evaluation: %s lost %d of %d verdict calls", metric, failed, len(results)
            )
        if failed * 2 > len(results):
            warnings.append(f"{metric} skipped: a majority of verdict calls failed.")
            return None
        return sum(successes) / len(successes)

    async def _faithfulness(
        self,
        *,
        question: str,
        answer: str,
        contexts: list[str],
        semaphore: asyncio.Semaphore,
        warnings: list[str],
    ) -> float | None:
        """Atomic-claim decomposition (one call) then per-claim support verdicts → x/total."""
        raw = await self._judge_json(
            semaphore,
            system=CLAIMS_SYSTEM,
            user=(
                f"Question: {question}\n\nAnswer: {answer}\n\n"
                f"Return at most {self._max_claims} claims."
            ),
        )
        claims = _parse_string_list(raw, "claims")
        if not claims:
            warnings.append(
                "faithfulness skipped: no factual claims could be extracted from the answer."
            )
            return None
        if len(claims) > self._max_claims:  # post-parse backstop behind the in-prompt cap
            warnings.append(
                f"faithfulness judged the first {self._max_claims} of {len(claims)} claims (cap)."
            )
            claims = claims[: self._max_claims]
        block = _context_block(contexts)

        async def _one(claim: str) -> bool:
            raw_verdict = await self._judge_json(
                semaphore,
                system=CLAIM_VERDICT_SYSTEM,
                user=f"Context:\n{block}\n\nClaim: {claim}",
            )
            return _parse_bool(raw_verdict, "supported")

        return await self._verdict_fraction(
            metric="faithfulness", calls=(_one(claim) for claim in claims), warnings=warnings
        )

    async def _answer_relevance(
        self, *, question: str, answer: str, semaphore: asyncio.Semaphore
    ) -> float:
        """One judging call: how directly the answer addresses the question → 0–1 (clamped)."""
        raw = await self._judge_json(
            semaphore,
            system=RELEVANCE_SYSTEM,
            user=f"Question: {question}\n\nAnswer: {answer}",
        )
        return _parse_score(raw)

    async def _context_precision(
        self,
        *,
        question: str,
        contexts: list[str],
        semaphore: asyncio.Semaphore,
        warnings: list[str],
    ) -> float | None:
        """Per-chunk relevance verdicts against the question → relevant/total (order-insensitive).

        ``contexts`` is already the once-capped judged set — the same set every metric sees.
        """

        async def _one(chunk: str) -> bool:
            raw = await self._judge_json(
                semaphore,
                system=PRECISION_SYSTEM,
                user=f"Question: {question}\n\nContext chunk:\n{chunk}",
            )
            return _parse_bool(raw, "relevant")

        return await self._verdict_fraction(
            metric="context_precision",
            calls=(_one(chunk) for chunk in contexts),
            warnings=warnings,
        )

    async def _context_recall(
        self,
        *,
        ground_truth: str,
        contexts: list[str],
        semaphore: asyncio.Semaphore,
        warnings: list[str],
    ) -> float | None:
        """Ground-truth statement decomposition then per-statement attribution → found/total."""
        raw = await self._judge_json(
            semaphore,
            system=STATEMENTS_SYSTEM,
            user=(
                f"Reference answer: {ground_truth}\n\nReturn at most {self._max_claims} statements."
            ),
        )
        statements = _parse_string_list(raw, "statements")
        if not statements:
            warnings.append(
                "context_recall skipped: no statements could be extracted from ground_truth."
            )
            return None
        if len(statements) > self._max_claims:  # post-parse backstop behind the in-prompt cap
            warnings.append(
                f"context_recall judged the first {self._max_claims} of "
                f"{len(statements)} statements (cap)."
            )
            statements = statements[: self._max_claims]
        block = _context_block(contexts)

        async def _one(statement: str) -> bool:
            raw_verdict = await self._judge_json(
                semaphore,
                system=RECALL_VERDICT_SYSTEM,
                user=f"Context:\n{block}\n\nStatement: {statement}",
            )
            return _parse_bool(raw_verdict, "attributable")

        return await self._verdict_fraction(
            metric="context_recall",
            calls=(_one(statement) for statement in statements),
            warnings=warnings,
        )
