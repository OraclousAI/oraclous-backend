"""The flow-level evaluation contract types (ADR-037 Decision 1 / E4 #469).

The ``Verdict`` is the structured judgement of a run/stage/member output against a ``Rubric``; the
``Rubric`` is the rubric-agnostic config the engine runs. These are the shapes #470 (named battery)
and E8 (closed loop) build on. ``pass`` is a Python keyword, so the model field is ``passed`` with
a ``pass`` JSON alias — callers serialize/parse ``{"pass": ...}``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["critical", "major", "minor"]
DimensionKind = Literal["llm_judge", "deterministic"]
TargetKind = Literal["run", "stage", "member_output"]
RecommendedAction = Literal["accept", "revise", "retry", "escalate_human", "reject"]


class Dimension(BaseModel):
    """One rubric dimension — an LLM-judged criterion or a deterministic check."""

    model_config = ConfigDict(extra="forbid")

    name: str
    prompt: str
    weight: float = 1.0
    threshold: float = 0.5
    severity: Severity = "major"
    # llm_judge → one judge call scores the criterion; deterministic → a coded predicate (the
    # core/check/<id> registry #470 ships; resolved via the evaluator's injected predicate hook).
    kind: DimensionKind = "llm_judge"


class Rubric(BaseModel):
    """The rubric-agnostic config the evaluator runs (one or many dimensions)."""

    model_config = ConfigDict(extra="forbid")

    dimensions: list[Dimension]
    pass_threshold: float = 0.7
    # Optional explicit AND-floor ordering; default = severity rank (critical first). Reserved for
    # the named-battery precedence (#470); the single-rubric path uses severity rank.
    precedence: list[str] = Field(default_factory=list)


class Failure(BaseModel):
    """A dimension that did not meet its threshold or could not be scored. ``reason`` is a
    label/short rationale — never verbatim customer manifest/output text (§3.7 / ADR-037 H5)."""

    model_config = ConfigDict(extra="forbid")

    dimension: str
    severity: Severity
    reason: str
    score: float | None = None


class Evaluated(BaseModel):
    """What was graded. ``organisation_id`` is server-stamped from the principal (ADR-037 H2) —
    the caller never supplies it on the request body."""

    model_config = ConfigDict(extra="forbid")

    target_kind: TargetKind
    target_ref: str
    organisation_id: str


class Verdict(BaseModel):
    """The structured judgement (ADR-037 Decision 1). Fail-closed: an ambiguous/partial result is
    ``passed = False``. ``dimension_scores`` excludes fail-soft nulls (those appear in ``warnings``
    + as ``failures`` with ``score = None``)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    score: float
    passed: bool = Field(alias="pass")
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    failures: list[Failure] = Field(default_factory=list)
    recommended_action: RecommendedAction
    metrics_computed: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evaluated: Evaluated
