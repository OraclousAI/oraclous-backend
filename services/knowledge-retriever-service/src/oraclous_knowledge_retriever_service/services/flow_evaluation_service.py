"""Flow-level evaluation use-case (ADR-037 / #469) — services layer.

Builds a ``Rubric`` from the manifest ``success_criteria`` and runs the shared ``packages/eval``
evaluator over the one judge, returning the typed ``Verdict``. Prose criteria → one holistic
``llm_judge`` dimension; a ``battery:<name>`` reference is the named-battery path (#470) — not yet
supported here, refused explicitly rather than silently mis-graded.
"""

from __future__ import annotations

from oraclous_eval import Dimension, Evaluated, Rubric, RubricEvaluator, Verdict

_BATTERY_PREFIX = "battery:"


class BatteryNotSupported(Exception):
    """``success_criteria`` references a named battery; that path lands with #470 (→ 422)."""


class FlowEvaluationService:
    def __init__(self, evaluator: RubricEvaluator) -> None:
        self._evaluator = evaluator

    async def evaluate(
        self,
        *,
        target_kind: str,
        target_ref: str,
        target_output: str,
        success_criteria: str,
        organisation_id: str,
        pass_threshold: float = 0.7,
    ) -> Verdict:
        if success_criteria.startswith(_BATTERY_PREFIX):
            raise BatteryNotSupported(success_criteria)
        rubric = Rubric(
            dimensions=[
                Dimension(name="success_criteria", prompt=success_criteria, kind="llm_judge")
            ],
            pass_threshold=pass_threshold,
        )
        evaluated = Evaluated(
            target_kind=target_kind,  # type: ignore[arg-type]  # validated by the request schema
            target_ref=target_ref,
            organisation_id=organisation_id,  # server-stamped by the caller (ADR-037 H2)
        )
        return await self._evaluator.evaluate(
            rubric=rubric, target_output=target_output, evaluated=evaluated
        )
