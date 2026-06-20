"""The rubric-agnostic evaluator (ADR-037 Decision 1 — the generalized KRS orchestration posture).

Runs an arbitrary :class:`Rubric` over a target output via the one judge, preserving the KRS safety
posture: per-request concurrency cap, an optional process-wide slots gate, a whole-evaluation
deadline that returns PARTIAL results (computed dimensions + nulls/warnings for the rest, never a
504-burn), per-dimension fail-soft (a judge failure nulls THAT dimension + warns, never a 500), and
strict score parsing (clamp [0,1], NaN/Inf reject — never fabricate). Assembles the typed Verdict.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from oraclous_eval.judge import EvalJudge
from oraclous_eval.parsing import JudgeResponseError, parse_reason, parse_score
from oraclous_eval.types import (
    Dimension,
    Evaluated,
    Failure,
    RecommendedAction,
    Rubric,
    Verdict,
)

_JUDGE_SYSTEM = (
    "You are a strict, impartial evaluator. Given a CRITERION and an OUTPUT, score from 0.0 to 1.0 "
    "how fully the output satisfies the criterion. Respond ONLY with a JSON object "
    '{"score": <number 0..1>, "reason": "<short rationale; do NOT quote the output verbatim>"}. '
    "Be conservative: when uncertain or when evidence is missing, score lower."
)

# A deterministic dimension is scored by an injected predicate (the core/check/<id> registry the
# named battery #470 ships). It returns a 0..1 score for (dimension, target_output).
DeterministicResolver = Callable[[Dimension, str], Awaitable[float] | float]


class EvaluationCapacityExceeded(Exception):
    """The process-wide evaluation slots are exhausted (→ the caller maps to a typed 429)."""


class RubricEvaluator:
    def __init__(
        self,
        judge: EvalJudge,
        *,
        max_concurrency: int = 4,
        deadline_seconds: float = 25.0,
        slots: asyncio.Semaphore | None = None,
    ) -> None:
        self._judge = judge
        self._max_concurrency = max(1, max_concurrency)
        self._deadline_seconds = deadline_seconds
        self._slots = slots

    async def evaluate(
        self,
        *,
        rubric: Rubric,
        target_output: str,
        evaluated: Evaluated,
        deterministic_resolver: DeterministicResolver | None = None,
    ) -> Verdict:
        scores: dict[str, float] = {}
        failures: list[Failure] = []
        warnings: list[str] = []
        sem = asyncio.Semaphore(self._max_concurrency)

        if self._slots is not None and self._slots.locked():
            raise EvaluationCapacityExceeded("evaluation slots exhausted")

        async def one(dim: Dimension) -> None:
            async with sem:
                try:
                    score = await self._score_dimension(dim, target_output, deterministic_resolver)
                except JudgeResponseError as exc:  # malformed/out-of-range → fail-soft null
                    warnings.append(f"{dim.name}: {exc}")
                    failures.append(
                        Failure(dimension=dim.name, severity=dim.severity, reason="uncomputable")
                    )
                    return
                except Exception as exc:  # noqa: BLE001 — judge/store error nulls the dim, never 500s
                    warnings.append(f"{dim.name}: judge error")
                    failures.append(
                        Failure(dimension=dim.name, severity=dim.severity, reason="judge error")
                    )
                    _ = exc
                    return
                scores[dim.name] = score
                if score < dim.threshold:
                    failures.append(
                        Failure(
                            dimension=dim.name,
                            severity=dim.severity,
                            reason="below threshold",
                            score=score,
                        )
                    )

        async with self._maybe_slot():
            try:
                async with asyncio.timeout(self._deadline_seconds):
                    await asyncio.gather(*(one(d) for d in rubric.dimensions))
            except TimeoutError:
                warnings.append(
                    f"evaluation deadline ({self._deadline_seconds:g}s) hit — partial result"
                )

        # any dimension neither scored nor already failed was cut by the deadline → null it
        accounted = set(scores) | {f.dimension for f in failures}
        for dim in rubric.dimensions:
            if dim.name not in accounted:
                failures.append(
                    Failure(dimension=dim.name, severity=dim.severity, reason="deadline")
                )

        return self._assemble(rubric, scores, failures, warnings, evaluated)

    async def _score_dimension(
        self, dim: Dimension, target_output: str, resolver: DeterministicResolver | None
    ) -> float:
        if dim.kind == "deterministic":
            if resolver is None:
                raise JudgeResponseError("no deterministic predicate resolver wired (#470)")
            out = resolver(dim, target_output)
            value = await out if isinstance(out, Awaitable) else out
            return max(0.0, min(1.0, float(value)))
        raw = await self._judge.complete_json(
            system=_JUDGE_SYSTEM,
            user=f"CRITERION:\n{dim.prompt}\n\nOUTPUT:\n{target_output}",
        )
        # reason parsed (label-only; never echoed verbatim into a failure reason — ADR-037 H5)
        _ = parse_reason(raw)
        return parse_score(raw)

    def _maybe_slot(self) -> _SlotGuard:
        return _SlotGuard(self._slots)

    def _assemble(
        self,
        rubric: Rubric,
        scores: dict[str, float],
        failures: list[Failure],
        warnings: list[str],
        evaluated: Evaluated,
    ) -> Verdict:
        if scores:
            weights = {d.name: d.weight for d in rubric.dimensions}
            total_w = sum(weights[n] for n in scores) or float(len(scores))
            score = round(sum(scores[n] * weights[n] for n in scores) / total_w, 4)
        else:
            score = 0.0  # fail-closed: nothing computed → 0
        has_critical = any(f.severity == "critical" for f in failures)
        passed = (score >= rubric.pass_threshold) and not has_critical
        action: RecommendedAction = (
            "escalate_human" if has_critical else ("accept" if passed else "revise")
        )
        return Verdict(
            score=score,
            **{"pass": passed},
            dimension_scores=scores,
            failures=failures,
            recommended_action=action,
            metrics_computed=list(scores),
            warnings=warnings,
            evaluated=evaluated,
        )


class _SlotGuard:
    """Acquire the optional process-wide slots semaphore for the evaluation lifetime."""

    def __init__(self, slots: asyncio.Semaphore | None) -> None:
        self._slots = slots

    async def __aenter__(self) -> None:
        if self._slots is not None:
            await self._slots.acquire()

    async def __aexit__(self, *exc: object) -> None:
        if self._slots is not None:
            self._slots.release()
