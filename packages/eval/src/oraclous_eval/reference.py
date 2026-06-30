"""#597 (ADR-047 §7) — the reference-objective eval-set (Layer-3 DATA).

~16 prose objectives spanning the shapes the compiler must handle (research, extraction, synthesis,
a standing monitor, a QA-gated pipeline, comparison, translation, …), each with the SPLIT rubric:
a plan-adequacy rubric (judged on the manifest) + a run-outcome rubric (judged on the deliverable).
The ship-bar is recorded here as data (the product call), NOT hard-coded in a test assertion —
K-of-N with a median-score floor + a judge-variance ceiling (ADR-047 founder-decision #3: ≥80%/N≥3).

This is the declarative spec the use-case-guardian runs at the M4 acceptance gate. It is versioned
data: changing the objectives or the bar is a reviewed change, not a code edit.
"""

from __future__ import annotations

from oraclous_eval.evalset import EvalSetManifest, Objective, ShipBar
from oraclous_eval.types import Dimension, Rubric


def _plan_rubric(prose: str) -> Rubric:
    """The plan-adequacy rubric — judged on the compiled MANIFEST alone (no run). Catches a
    structurally-wrong team cheaply before a costly run."""
    return Rubric(
        pass_threshold=0.7,
        dimensions=[
            Dimension(
                name="role-coverage",
                severity="critical",
                prompt=(
                    f"Objective: {prose}\nThe input is a compiled team manifest (JSON). Score 0–1: "
                    "do the members cover the roles this objective needs, with no missing role and "
                    "no irrelevant member?"
                ),
            ),
            Dimension(
                name="capability-fit",
                severity="critical",
                prompt=(
                    f"Objective: {prose}\nScore 0–1: does each member declare the tools its "
                    "sub-goal needs (and no tool the objective clearly does not require)?"
                ),
            ),
            Dimension(
                name="dag-sanity",
                severity="major",
                prompt=(
                    f"Objective: {prose}\nScore 0–1: is the members' depends_on ordering "
                    "sensible — producers before the consumers that use their output?"
                ),
            ),
            Dimension(
                name="subgoal-coverage",
                severity="major",
                prompt=(
                    f"Objective: {prose}\nScore 0–1: taken together, do the members' sub-goals "
                    "cover the whole objective with no gap?"
                ),
            ),
        ],
    )


def _run_rubric(criteria: str) -> Rubric:
    """The run-outcome rubric — judged on the executed deliverable."""
    return Rubric(
        pass_threshold=0.7,
        dimensions=[
            Dimension(
                name="completeness",
                severity="critical",
                prompt=f"Score 0–1: does the deliverable fully deliver on: {criteria}?",
            ),
            Dimension(
                name="usability",
                severity="major",
                prompt=f"Score 0–1: is the deliverable a usable result for {criteria}?",
            ),
        ],
    )


def _objective(obj_id: str, prose: str, run_criteria: str) -> Objective:
    return Objective(
        id=obj_id,
        prose=prose,
        plan_rubric=_plan_rubric(prose),
        run_rubric=_run_rubric(run_criteria),
    )


# (id, prose objective, run-outcome criteria) — the reference set the guardian runs.
_OBJECTIVES: tuple[tuple[str, str, str], ...] = (
    (
        "research-digest",
        "Research the week's top AI news and write a short plain-text digest.",
        "a concise, readable digest of recent AI news",
    ),
    (
        "competitive-analysis",
        "Compare three project-management tools and recommend one with a pros/cons table.",
        "a comparison table plus a clear recommendation",
    ),
    (
        "data-extraction",
        "Extract every company name and funding amount from a batch of press releases into a list.",
        "a structured list of company/funding pairs",
    ),
    (
        "doc-summary",
        "Summarize a long technical document into an executive summary with key takeaways.",
        "an executive summary with bulleted key takeaways",
    ),
    (
        "faq-builder",
        "From a product's support transcripts, draft a ten-question FAQ with answers.",
        "ten relevant Q&A pairs grounded in the transcripts",
    ),
    (
        "code-review",
        "Review a Python module for bugs and produce a prioritized findings list.",
        "a prioritized list of findings with severities",
    ),
    (
        "content-calendar",
        "Plan a one-month social content calendar for a developer-tools brand.",
        "a dated calendar of post ideas across the month",
    ),
    (
        "release-notes",
        "Turn a list of merged pull requests into customer-facing release notes.",
        "grouped, customer-readable release notes",
    ),
    (
        "literature-review",
        "Survey recent papers on retrieval-augmented generation and synthesize the approaches.",
        "a synthesis of the main RAG approaches with distinctions",
    ),
    (
        "data-quality-gate",
        "Validate a CSV of transactions and flag every row that fails an integrity check.",
        "a list of flagged rows with the failed check named",
    ),
    (
        "translation-review",
        "Translate a set of marketing snippets into Spanish and review them for tone.",
        "accurate Spanish translations with a tone note per snippet",
    ),
    (
        "incident-postmortem",
        "From an incident timeline, draft a blameless postmortem with action items.",
        "a blameless postmortem ending in concrete action items",
    ),
    (
        "survey-synthesis",
        "Aggregate open-ended survey responses into themes with representative quotes.",
        "named themes each backed by a representative quote",
    ),
    (
        "onboarding-guide",
        "Write a step-by-step onboarding guide for a new REST API from its specification.",
        "an ordered, runnable onboarding guide for the API",
    ),
    (
        "comparison-matrix",
        "Build a feature comparison matrix for four cloud databases.",
        "a four-column feature matrix with consistent rows",
    ),
    (
        "meeting-notes",
        "Turn a raw meeting transcript into structured notes with decisions and owners.",
        "structured notes listing decisions and their owners",
    ),
)


def reference_eval_set() -> EvalSetManifest:
    """The recorded reference eval-set: 16 objectives + the ADR-047 §7 ship-bar (N=3, ≥80% pass)."""
    return EvalSetManifest(
        name="e10-compiler-reference",
        objectives=[_objective(i, prose, crit) for (i, prose, crit) in _OBJECTIVES],
        # ADR-047 founder-decision #3: ≥80% of the objectives must pass K-of-N (N≥3). 2-of-3 per
        # objective; a median-score floor; a judge-variance ceiling beyond which the call is
        # inconclusive (never a silent ship). The product owns these numbers — recorded here.
        ship_bar=ShipBar(
            n_samples=3, k_pass=2, min_score=0.7, max_variance=0.2, objective_pass_ratio=0.8
        ),
    )
