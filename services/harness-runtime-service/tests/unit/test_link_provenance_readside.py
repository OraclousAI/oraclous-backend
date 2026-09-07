"""#944 — how an unverified inline link reaches a reader, read-side.

The check runs inside the loop and records its verdict in the trace. This file pins the DTO that
carries that verdict out of the harness, so the engine (and through it the console) can act on it.

**No new database column, and no migration.** The verdict is derived from the persisted trace, the
posture ``driving_signals`` (#642) and ``simulated`` (#907) already use. That is not merely cheaper:
a computed field cannot drift from the trace it is computed from, whereas a column written beside
the trace can, and a run whose column says "clean" over a trace that says otherwise is worse than
no flag at all.

**Why the FLAG step and not the URLs in the answer.** Deriving read-side by re-running the check
over the stored answer is not possible: a step's ``detail`` is truncated at persistence, so the set
of URLs the run actually fetched cannot be reconstructed from the stored trace. The loop is the only
place that ever holds the full tool results, so the loop is where the verdict must be reached. Read
side only reports it.

**Truncation is why the STATUS carries the boolean and the DETAIL carries the list.** An answer that
invented twenty links produces a detail string longer than the trace's 500-character step budget, so
the list is best-effort. The status is a short fixed token, so "this answer has unverified links"
survives truncation intact. A reader that only needs the warning is never misled; a reader that
wants the specific URLs may get a shortened list, which is the right way round.

Every import here is a shipped seam, so collection stays clean
(``.claude/rules/tests-seam-imports.md``); the assertions fail RED until the ``[impl]`` lands.
"""

from __future__ import annotations

import json
import uuid

import pytest
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind
from oraclous_harness_runtime_service.schema.harness_schemas import HarnessExecutionOut, StepOut

pytestmark = [pytest.mark.unit]

_FABRICATED = "https://www.okta.com/blog/2023/10/okta-ai-token-costs"
_ALSO_FABRICATED = "https://www.forbes.com/sites/nobody/2026/01/01/invented/"

_GATE_NAME = "link_provenance"
_FLAG_STATUS = "unverified_links"
_CORRECTION_STATUS = "link_correction"


def _execution_out(steps: list[StepOut]) -> HarnessExecutionOut:
    return HarnessExecutionOut(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        harness_name="linker",
        content_hash=None,
        status=HarnessStatus.SUCCEEDED,
        output="done",
        error_type=None,
        error_message=None,
        iterations=2,
        total_tokens=0,
        steps=steps,
        created_at=None,
    )


def _flag(*urls: str) -> StepOut:
    return StepOut(
        index=1,
        kind=StepKind.GATE,
        name=_GATE_NAME,
        status=_FLAG_STATUS,
        detail=json.dumps(list(urls)),
    )


def test_the_dto_reports_the_urls_named_by_the_flag_step() -> None:
    out = _execution_out(
        [
            StepOut(index=0, kind=StepKind.LLM, name="primary", status="answer"),
            _flag(_FABRICATED, _ALSO_FABRICATED),
        ]
    )
    assert out.unverified_links == [_FABRICATED, _ALSO_FABRICATED]


def test_a_clean_run_reports_an_empty_list_never_none() -> None:
    # Empty, never None, for the reason served_citation_ids is: a caller reads this on every run,
    # and None would make "nothing was wrong" indistinguishable from "the harness forgot to check".
    out = _execution_out([StepOut(index=0, kind=StepKind.LLM, name="primary", status="answer")])
    assert out.unverified_links == []


def test_a_correction_step_alone_does_not_flag_the_run() -> None:
    # A member that was corrected and then cited honestly shipped a clean answer. Reporting it as
    # unverified would warn a reader about a link that is not in what they are reading.
    out = _execution_out(
        [
            StepOut(
                index=0,
                kind=StepKind.GATE,
                name=_GATE_NAME,
                status=_CORRECTION_STATUS,
                detail=json.dumps([_FABRICATED]),
            ),
            StepOut(index=1, kind=StepKind.LLM, name="primary", status="answer"),
        ]
    )
    assert out.unverified_links == []


def test_a_pre_change_trace_with_no_gate_step_parses_and_reports_clean() -> None:
    # Back-compat, the #641/#907 posture: every trace persisted before this change still validates
    # and reports an empty list rather than crashing the read.
    out = HarnessExecutionOut.model_validate(
        {
            "id": uuid.uuid4(),
            "organisation_id": uuid.uuid4(),
            "harness_id": uuid.uuid4(),
            "harness_name": "linker",
            "content_hash": None,
            "status": HarnessStatus.SUCCEEDED,
            "output": "done",
            "error_type": None,
            "error_message": None,
            "iterations": 1,
            "total_tokens": 0,
            "steps": [{"index": 0, "kind": "llm", "name": "primary", "status": "answer"}],
            "created_at": None,
        }
    )
    assert out.unverified_links == []


def test_a_truncated_detail_still_reports_the_run_as_flagged() -> None:
    # The step budget cut the JSON list mid-string. The warning must survive that — a reader told
    # nothing because the list would not fit is the silent trust this issue exists to remove.
    truncated = json.dumps([_FABRICATED, _ALSO_FABRICATED])[:60] + "…"
    out = _execution_out(
        [
            StepOut(
                index=0, kind=StepKind.GATE, name=_GATE_NAME, status=_FLAG_STATUS, detail=truncated
            )
        ]
    )
    assert out.has_unverified_links is True


def test_has_unverified_links_is_false_on_a_clean_run() -> None:
    out = _execution_out([StepOut(index=0, kind=StepKind.LLM, name="primary", status="answer")])
    assert out.has_unverified_links is False
