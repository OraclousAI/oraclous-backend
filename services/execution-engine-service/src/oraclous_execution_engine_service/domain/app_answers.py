"""#846 — the validation desk's intake answers, on their way into a team run's ``inputs``.

**One-off, and deliberately so.** ADR-052 decision 3 (``oraclous-knowledge``, app-descriptor layer
for generated apps) rules that an app's own input shape belongs in a descriptor layer sitting in
front of the run manifest, and that "this input is a hypothesis, not a premise" becomes a general
platform concept. Neither exists yet, and Contract #845 blocked the validation desk's
approve-starts-the-run step on them landing first. So this ships that app's own field now —
``inputs["answers"]``, a list of ``{question, answer, hypothesis}`` — private to that app,
documented as temporary, and expected to migrate onto the descriptor layer once it is built.

Why it needs a home at all: ``validate_input_keys`` fail-closes any ``inputs`` key the team
manifest does not declare (#714 defect (b)), so the field is a 422 without this. And a key that
merely passes that gate is worse than useless — the three readers of ``inputs`` (the declared
``task_input.key``, a member's ``fan_out.over``, the engine-reserved ``_refresh_seed``) would none
of them touch it, so the founder's answer would be silently discarded and the model would fill the
hole with its own guess. That is exactly the failure #714 closed. The answers are therefore
RENDERED into every member's input (``team_run.render_member_input``), the flagged ones under a
directive that forbids treating them as established.
"""

from __future__ import annotations

from typing import Any

#: The ``inputs`` key the validation desk's intake rides under. Unprefixed because ADR-052 spells it
#: that way (``maps_to: answers``). A team could therefore also declare ``task_input.key:
#: "answers"`` or ``fan_out.over: "answers"``; ``_consumed_input_keys`` holds a SET, so that is not
#: an error — the same value would simply be read twice, once as the task and once as the answer
#: list. Acceptable for a field with a scheduled removal date, and it disappears with the migration.
ANSWERS_KEY = "answers"

#: The per-item flag: "the user did not know this, so test it — never assume it".
HYPOTHESIS_FIELD = "hypothesis"

#: What the 422 says. The SHAPE only — never a fragment of what the user typed (CLAUDE.md §11).
_SHAPE = (
    f"inputs[{ANSWERS_KEY!r}] must be a list of objects, each with a non-empty string 'question', "
    "an 'answer' that is a string or null, and an optional boolean 'hypothesis'"
)


def _validate_item(item: Any) -> bool:  # noqa: ANN401 — arbitrary caller JSON by definition
    """One item's shape, fail-closed (CLAUDE.md §3.5): its ``hypothesis`` flag decides whether a
    member is told to trust it, so a shape the renderer cannot read is rejected at create rather
    than half-rendered. Returns the resolved flag."""
    if not isinstance(item, dict):
        raise ValueError(_SHAPE)
    question = item.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError(_SHAPE)
    answer = item.get("answer")
    if answer is not None and not isinstance(answer, str):
        raise ValueError(_SHAPE)
    flag = item.get(HYPOTHESIS_FIELD, False)
    if not isinstance(flag, bool):
        raise ValueError(_SHAPE)
    return flag


def parse_answers(
    inputs: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Split ``inputs["answers"]`` into ``(confirmed, hypotheses)``.

    ``None`` when the key is absent or the list is empty — the default-OFF signal that keeps a run
    without the field rendering byte-for-byte as it does today (the #602/#674 discipline). Raises
    ``ValueError`` on a malformed payload; the service layer maps that to a 422 at create, before
    the run is persisted or enqueued.
    """
    raw = (inputs or {}).get(ANSWERS_KEY)
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(_SHAPE)
    confirmed: list[dict[str, Any]] = []
    hypotheses: list[dict[str, Any]] = []
    for item in raw:
        (hypotheses if _validate_item(item) else confirmed).append(item)
    if not confirmed and not hypotheses:
        return None  # an empty list is the same as no field at all
    return confirmed, hypotheses
