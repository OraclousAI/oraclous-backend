"""#641/#642 — claims need receipts: driving_signals grounding + the strict success grade.

Run ``1fe1bcb5`` (team ``compiled-1320067b``): the analyzer's "answer" was an echoed JSON
tool-call plan — never dispatched — and it was marked succeeded; the collector's only call
returned ``unknown_tool`` and it was marked succeeded too. Nothing links a member's claim to a
tool call that actually ran.

The contract these tests pin (RED until the [impl] lands):

* ``validate_grounding(driving_signals, tool_steps)`` (new, ``oraclous_ohm.envelope``) — every
  claim must carry a ``source_tool_call_id`` resolving to a ``status == "ok"`` tool step in the
  member's OWN trace; missing / null / unresolved / errored-call references are errors.
* ``run_team`` grades a TOOL-DECLARING member (non-empty ``member.tools``) STRICTLY (user
  decision 2026-07-27, no degraded middle state): succeeded ONLY when ≥1 ok tool step exists AND
  every driving_signal resolves to an ok step — otherwise ``failed`` (which keeps the member
  re-runnable via the existing rerun path). Zero-tools members keep their prior semantics.
* ``TeamRunResult.member_grounding`` carries ``{"grounded": int, "total": int}`` per
  tool-declaring member so the engine can persist a run-level grounding score.

New-symbol imports are function-local (§4.1) so collection never breaks other suites.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import (
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMRuntime,
)
from oraclous_ohm.orchestrate import run_team

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(role: str, deps: list[str] | None = None, tools: list[str] | None = None) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        depends_on=deps or [],
        tools=tools or [],
    )


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


def _ok_steps() -> list[dict[str, Any]]:
    return [
        {
            "index": 1,
            "kind": "tool",
            "name": "github.list_commits",
            "status": "ok",
            "tool_call_id": "tc-1",
        }
    ]


def _signals(call_id: str | None = "tc-1") -> list[dict[str, Any]]:
    return [{"signal": "commit_count", "value": 12, "source_tool_call_id": call_id}]


# --- validate_grounding: the pure claim→receipt check (#641) ----------------------------------
def test_validate_grounding_accepts_a_claim_backed_by_an_ok_call() -> None:
    from oraclous_ohm.envelope import validate_grounding

    assert validate_grounding(_signals(), _ok_steps()) == []


def test_validate_grounding_rejects_missing_or_empty_signals() -> None:
    # A tool-declaring member with NO driving_signals made claims nothing backs — fail-closed.
    from oraclous_ohm.envelope import validate_grounding

    assert validate_grounding(None, _ok_steps()) != []
    assert validate_grounding([], _ok_steps()) != []


def test_validate_grounding_rejects_a_null_or_missing_source_id() -> None:
    from oraclous_ohm.envelope import validate_grounding

    assert validate_grounding(_signals(None), _ok_steps()) != []
    assert validate_grounding([{"signal": "s", "value": 1}], _ok_steps()) != []


def test_validate_grounding_rejects_an_unresolved_source_id() -> None:
    # The id must resolve within the member's OWN run tree — a copied/invented id is no receipt.
    from oraclous_ohm.envelope import validate_grounding

    assert validate_grounding(_signals("tc-999"), _ok_steps()) != []


def test_the_poc_invented_receipt_id_is_rejected_by_grounding() -> None:
    # The #734 PoC's invented receipt id (`source_tool_call_id=call_8f3a2b`), pinned at its real
    # home per the #788 ruling (option B, 2026-08-12): §CITE rule 1 no longer watches this token,
    # because THIS layer resolves it against the member's own trace and can tell whether the cited
    # call actually ran — which a prose regex never could. Green before the ruling too: #642
    # already caught it; the ruling revealed the coverage belonged here, not that it was missing.
    from oraclous_ohm.envelope import validate_grounding

    assert validate_grounding(_signals("call_8f3a2b"), _ok_steps()) != []


def test_validate_grounding_rejects_a_claim_backed_by_a_failed_call() -> None:
    from oraclous_ohm.envelope import validate_grounding

    errored = [
        {
            "index": 1,
            "kind": "tool",
            "name": "github.list_commits",
            "status": "error",
            "tool_call_id": "tc-1",
            "detail": '{"error":"unknown_tool"}',
        }
    ]
    assert validate_grounding(_signals("tc-1"), errored) != []


# --- run_team: the strict grounded grade (#642) -----------------------------------------------
def _dispatch_returning(out: dict[str, Any]):
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return dict(out)

    return dispatch


async def test_tool_declaring_member_with_grounded_claims_succeeds() -> None:
    res = await run_team(
        _team([_m("analyzer", tools=["github"])]),
        _dispatch_returning(
            {
                "output": "12 commits this week",
                "status": "SUCCEEDED",
                "steps": _ok_steps(),
                "driving_signals": _signals(),
            }
        ),
    )
    assert res.member_status == {"analyzer": "succeeded"}
    assert res.status == "completed"


async def test_regression_run_1fe1bcb5_analyzer_plan_as_answer_fails() -> None:
    # The member's "answer" is an echoed tool-call PLAN; no call was ever dispatched (no steps,
    # no signals). It used to be marked succeeded — it must now FAIL, and fail the team verdict.
    res = await run_team(
        _team([_m("analyzer", tools=["github"])]),
        _dispatch_returning(
            {
                "output": '{"tool":"github","action":"list_commits","args":{}}',
                "status": "SUCCEEDED",
            }
        ),
    )
    assert res.member_status == {"analyzer": "failed"}
    assert res.status == "failed"
    assert "grounding" in res.member_errors["analyzer"].lower()


async def test_regression_run_1fe1bcb5_collector_only_errored_calls_fails() -> None:
    # The collector's only call returned unknown_tool — claims pointing at it are unbacked.
    errored = [
        {
            "index": 1,
            "kind": "tool",
            "name": "github.list_commits",
            "status": "error",
            "tool_call_id": "tc-1",
            "detail": '{"error":"unknown_tool"}',
        }
    ]
    res = await run_team(
        _team([_m("collector", tools=["github"])]),
        _dispatch_returning(
            {
                "output": "found 12 commits",
                "status": "SUCCEEDED",
                "steps": errored,
                "driving_signals": _signals("tc-1"),
            }
        ),
    )
    assert res.member_status == {"collector": "failed"}
    assert res.status == "failed"


async def test_one_unresolved_claim_fails_the_member_strictly() -> None:
    # Strict grade (user decision): SOME receipts is not enough — every claim needs one.
    res = await run_team(
        _team([_m("analyzer", tools=["github"])]),
        _dispatch_returning(
            {
                "output": "two findings",
                "status": "SUCCEEDED",
                "steps": _ok_steps(),
                "driving_signals": _signals() + _signals("tc-invented"),
            }
        ),
    )
    assert res.member_status == {"analyzer": "failed"}


async def test_zero_tool_member_keeps_prior_status_semantics() -> None:
    # A pure-reasoning member (approval-gate style, no tool chips) is exempt — unchanged (#642).
    res = await run_team(
        _team([_m("thinker")]), _dispatch_returning({"output": "reasoned", "status": "SUCCEEDED"})
    )
    assert res.member_status == {"thinker": "succeeded"}
    assert res.status == "completed"


async def test_member_grounding_counts_surface_on_the_result() -> None:
    res = await run_team(
        _team([_m("analyzer", tools=["github"]), _m("thinker", ["analyzer"])]),
        _dispatch_returning(
            {
                "output": "12 commits",
                "status": "SUCCEEDED",
                "steps": _ok_steps(),
                "driving_signals": _signals(),
            }
        ),
    )
    # Only the tool-declaring member is graded; the zero-tools member contributes no bucket.
    assert res.member_grounding == {"analyzer": {"grounded": 1, "total": 1}}
