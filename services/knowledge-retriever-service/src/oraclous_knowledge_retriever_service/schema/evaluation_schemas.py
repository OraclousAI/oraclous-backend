"""Evaluation DTOs (schema layer — Pydantic only) (#331).

Lifts the legacy evaluation endpoint's request/response SHAPE (the old knowledge-graph-builder
``evaluation_schemas.py``) — NOT its ragas/langchain implementation. Question/answer/ground-truth
lengths are bounded (cost control: they feed judge prompts); empty strings are rejected at the
DTO (min_length=1) rather than silently judged or ignored (#333).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field


class EvaluationRequest(BaseModel):
    """Request body for POST /v1/graph/{graph_id}/evaluate."""

    question: str = Field(
        min_length=1,
        max_length=10_000,
        description="The question to evaluate against the knowledge graph.",
    )
    answer: str | None = Field(
        default=None,
        min_length=1,
        max_length=10_000,
        description=(
            "The answer to evaluate. If omitted, one is generated from the retrieved context "
            "(retrieve → grounded-answer prompt → the judge LLM). Note the self-judging bias on "
            "that path: the same model writes and grades the answer."
        ),
    )
    ground_truth: str | None = Field(
        default=None,
        min_length=1,
        max_length=10_000,
        description=(
            "Reference answer for context_recall scoring. When omitted, context_recall is skipped."
        ),
    )
    metrics: list[Annotated[str, Field(max_length=64)]] | None = Field(
        default=None,
        max_length=8,
        description=(
            "Subset of metrics to compute. Allowed: faithfulness, answer_relevance, "
            "context_precision, context_recall. Defaults to all metrics applicable given the "
            "provided inputs; unknown names are ignored with a warning. An explicit empty list "
            "leaves nothing to compute → 422 (no_valid_metrics)."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "question": "Who is the CEO of Acme Corp?",
                "ground_truth": "John Smith is the CEO of Acme Corp.",
                "metrics": ["faithfulness", "answer_relevance", "context_precision"],
            }
        }
    }


class EvaluationScores(BaseModel):
    """Metric scores, 0–1 rounded to 4 dp (None when a metric was not computed)."""

    faithfulness: float | None = Field(
        default=None,
        description="Fraction of answer claims supported by the retrieved context (0-1).",
    )
    answer_relevance: float | None = Field(
        default=None,
        description=(
            "How directly the answer addresses the question (0-1). A direct judge score — not "
            "RAGAS's generated-questions + cosine-similarity estimator."
        ),
    )
    context_precision: float | None = Field(
        default=None,
        description=(
            "Fraction of judged chunks relevant to the question (0-1). Order-INsensitive — "
            "RAGAS's original context_precision is rank-weighted."
        ),
    )
    context_recall: float | None = Field(
        default=None,
        description=(
            "Fraction of ground-truth statements attributable to the retrieved context (0-1). "
            "Requires ground_truth."
        ),
    )


class RetrievedContextItem(BaseModel):
    """A single retrieved context chunk the metrics judged against."""

    node_id: str | None = None
    node_labels: list[str] | None = None
    content: str
    relevance_score: float | None = None


class EvaluationResponse(BaseModel):
    """Response body for POST /v1/graph/{graph_id}/evaluate."""

    graph_id: str
    question: str
    answer: str | None = Field(
        default=None,
        description=(
            "The answer that was evaluated (caller-supplied or generated). None when no "
            "answer-dependent metric was requested, or generation failed (see warnings)."
        ),
    )
    retrieved_contexts: list[RetrievedContextItem] = Field(
        default_factory=list,
        description=(
            "The judged context set: graph chunks from the existing KRS hybrid path, capped ONCE "
            "at eval_max_contexts (warned when the cap drops chunks) so every metric judges the "
            "same set."
        ),
    )
    scores: EvaluationScores
    overall: float | None = Field(
        default=None,
        description="Mean of the computed (non-null) scores, rounded to 4 dp.",
    )
    metrics_computed: list[str] = Field(
        default_factory=list,
        description="The metrics that actually produced a score (fail-soft nulls are excluded).",
    )
    is_grounded: bool = Field(
        description="True iff faithfulness was computed and meets the configured threshold.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Non-fatal issues encountered during evaluation: skipped metrics, caps, partial "
            "verdict-batch failures, and deadline-expired metrics (partial results)."
        ),
    )
