"""oraclous-eval — the shared flow-level evaluation engine (ADR-037 / E4).

One LLM-as-judge seam + a rubric-agnostic evaluator producing the typed ``Verdict``. Generalized out
of the KRS retrieval judge (#331); consumed by ``core/evaluate`` (#469), the named gate battery
(#470), and KRS's retrieval rubric (a thin caller).
"""

from oraclous_eval.evaluator import (
    DeterministicResolver,
    EvaluationCapacityExceeded,
    RubricEvaluator,
)
from oraclous_eval.judge import EvalJudge, JudgeConfig, OpenAIEvalJudge, make_judge
from oraclous_eval.parsing import JudgeResponseError, parse_json_object, parse_reason, parse_score
from oraclous_eval.types import (
    Dimension,
    Evaluated,
    Failure,
    RecommendedAction,
    Rubric,
    Severity,
    Verdict,
)

__all__ = [
    "DeterministicResolver",
    "Dimension",
    "EvalJudge",
    "Evaluated",
    "EvaluationCapacityExceeded",
    "Failure",
    "JudgeConfig",
    "JudgeResponseError",
    "OpenAIEvalJudge",
    "RecommendedAction",
    "Rubric",
    "RubricEvaluator",
    "Severity",
    "Verdict",
    "make_judge",
    "parse_json_object",
    "parse_reason",
    "parse_score",
]
