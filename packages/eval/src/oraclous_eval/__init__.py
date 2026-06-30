"""oraclous-eval — the shared flow-level evaluation engine (ADR-037 / E4) + the compiler eval-set.

One LLM-as-judge seam + a rubric-agnostic evaluator producing the typed ``Verdict``. Generalized out
of the KRS retrieval judge (#331); consumed by ``core/evaluate`` (#469), the named gate battery
(#470), and KRS's retrieval rubric (a thin caller). #597 (ADR-047 §7) adds the **E10 compiler
eval-set** — Layer-1 deterministic plan guardrails, Layer-2 the EURail-ledger equivalence oracle,
and Layer-3 the reference-objective ship-bar runner (sample-N, judge-debias, K-of-N).
"""

from oraclous_eval.equivalence import (
    BaselineBand,
    EquivalenceVerdict,
    build_report_editor_battery,
    count_ledger_records,
    ledger_equivalence,
)
from oraclous_eval.evalset import (
    CompiledPlan,
    DebiasedScore,
    EvalSetManifest,
    EvalSetResult,
    EvalSetRunner,
    Objective,
    SampleVerdict,
    ShipBar,
    ShipBarVerdict,
)
from oraclous_eval.evaluator import (
    DeterministicResolver,
    EvaluationCapacityExceeded,
    RubricEvaluator,
)
from oraclous_eval.guardrails import GuardrailCheck, GuardrailReport, run_plan_guardrails
from oraclous_eval.judge import EvalJudge, JudgeConfig, OpenAIEvalJudge, make_judge
from oraclous_eval.parsing import JudgeResponseError, parse_json_object, parse_reason, parse_score
from oraclous_eval.reference import reference_eval_set
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
    "BaselineBand",
    "CompiledPlan",
    "DebiasedScore",
    "DeterministicResolver",
    "Dimension",
    "EquivalenceVerdict",
    "EvalJudge",
    "EvalSetManifest",
    "EvalSetResult",
    "EvalSetRunner",
    "Evaluated",
    "EvaluationCapacityExceeded",
    "Failure",
    "GuardrailCheck",
    "GuardrailReport",
    "JudgeConfig",
    "JudgeResponseError",
    "Objective",
    "OpenAIEvalJudge",
    "RecommendedAction",
    "Rubric",
    "RubricEvaluator",
    "SampleVerdict",
    "Severity",
    "ShipBar",
    "ShipBarVerdict",
    "Verdict",
    "build_report_editor_battery",
    "count_ledger_records",
    "ledger_equivalence",
    "make_judge",
    "parse_json_object",
    "parse_reason",
    "parse_score",
    "reference_eval_set",
    "run_plan_guardrails",
]
