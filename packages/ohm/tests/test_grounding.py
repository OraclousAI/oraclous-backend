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
    OHMFanOut,
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


# --- #685: the message must name what actually failed -------------------------------------------
#
# Live 2026-08-25, run eb08c17d (the "Validation Desk"): the researcher made four search calls,
# every one came back 432 — the search provider's plan/usage-limit status — and it answered
# honestly that it had reached no sources. The run failed with:
#
#     grounding: no driving_signals: the member made claims nothing backs
#
# The member made no claims. It reported failure, with receipts for the failures. FAILING it is
# correct and stays correct — with no successful call there is nothing to ground (fail-closed).
# The defect is diagnostic: the operator-facing sentence accuses the member of fabrication, which
# sent the investigation into the grounding rules when the real cause was one billing fact.
#
# `validate_grounding` currently collapses three different empty-signal conditions into that one
# accusation. These tests pull them apart:
#
#   A. tool calls were made, NONE succeeded  -> say so, and name the last failure
#   B. no tool call was made at all          -> say that instead
#   C. a call DID succeed, still no claims   -> the old wording is accurate; keep it
#
# Paired with #875 (which classifies the provider status), case A is what finally puts the real
# cause — an exhausted search key — on the run page.

_OLD_ACCUSATION = "made claims nothing backs"


def _errored_steps(n: int = 1, *, detail: str = '{"error":"unknown_tool"}') -> list[dict[str, Any]]:
    return [
        {
            "index": i,
            "kind": "tool",
            "name": "web.search",
            "status": "error",
            "tool_call_id": f"tc-{i}",
            "detail": detail,
        }
        for i in range(1, n + 1)
    ]


def test_all_calls_errored_is_not_reported_as_fabrication() -> None:
    """Case A. The member cited its failures honestly; the message must not call that a claim."""
    from oraclous_ohm.envelope import validate_grounding

    errors = validate_grounding([], _errored_steps(4))
    assert errors, "no successful call means nothing to ground — this must still FAIL"
    message = " ".join(errors)
    assert _OLD_ACCUSATION not in message


def test_all_calls_errored_names_the_count_and_the_failing_tool() -> None:
    """An operator reading it should know how many calls ran and which tool broke."""
    from oraclous_ohm.envelope import validate_grounding

    message = " ".join(validate_grounding([], _errored_steps(4)))
    assert "4" in message, f"the call count is missing from {message!r}"
    assert "web.search" in message, f"the failing tool is missing from {message!r}"


def test_all_calls_errored_carries_the_last_error() -> None:
    """The live case: this is where the real cause reaches the run page.

    With #875 landed, a spent search key produces exactly this detail, so the operator reads
    'quota' instead of being told the member fabricated something.
    """
    from oraclous_ohm.envelope import validate_grounding

    steps = _errored_steps(2, detail='{"error":"the web-search credential has no remaining quota"}')
    message = " ".join(validate_grounding([], steps))
    assert "quota" in message.lower()


def test_the_last_error_is_the_last_one_not_the_first() -> None:
    """'last error' has to mean the most recent attempt, or it names the wrong cause."""
    from oraclous_ohm.envelope import validate_grounding

    steps = _errored_steps(1, detail='{"error":"FIRST-FAILURE"}')
    steps[0]["name"] = "github.list_commits"  # a different tool, so ordering can't pass by luck
    steps += [
        {
            "index": 2,
            "kind": "tool",
            "name": "web.search",
            "status": "error",
            "tool_call_id": "tc-2",
            "detail": '{"error":"LAST-FAILURE"}',
        }
    ]
    message = " ".join(validate_grounding([], steps))
    assert "LAST-FAILURE" in message
    assert "FIRST-FAILURE" not in message


def test_the_error_excerpt_is_bounded() -> None:
    """A tool result can be arbitrarily long. The run page must not swallow one whole.

    300, not 600 (ruled on review). Two bounds sit downstream of this one. The stored detail is
    already capped at 500 where the trace is built, so a 600-char message would admit the whole
    body plus a frame — "a bounded excerpt" and "all of it" would be the same implementation.
    More importantly every failed member shares ONE 2000-char budget on the run page, so a
    generous per-member message silently cuts later members off the page entirely. At 300 six
    failed members still fit, and the live payload ("the web-search credential has no remaining
    quota") is 47 characters — roughly four times the real signal is still available.
    """
    from oraclous_ohm.envelope import validate_grounding

    steps = _errored_steps(1, detail="x" * 10_000)
    message = " ".join(validate_grounding([], steps))
    assert len(message) < 300, (
        f"the message grew to {len(message)} chars — the excerpt is unbounded"
    )


def test_no_tool_call_at_all_reads_differently_from_all_calls_failed() -> None:
    """Case B. 'It never tried' and 'it tried four times and every one broke' are different
    problems with different fixes, and the operator has to be able to tell them apart."""
    from oraclous_ohm.envelope import validate_grounding

    never_tried = " ".join(validate_grounding([], []))
    all_failed = " ".join(validate_grounding([], _errored_steps(4)))
    assert never_tried, "a member that called nothing still has nothing to ground — still FAILS"
    assert never_tried != all_failed
    assert _OLD_ACCUSATION not in never_tried


def test_a_successful_call_with_no_claims_keeps_the_old_wording() -> None:
    """Case C. Here the accusation is ACCURATE — the member had a working call and cited nothing.
    #685 narrows that sentence to the case it was written for; it does not delete it."""
    from oraclous_ohm.envelope import validate_grounding

    message = " ".join(validate_grounding([], _ok_steps()))
    assert _OLD_ACCUSATION in message


def test_a_mixed_trace_with_one_ok_call_is_still_case_c() -> None:
    """One call succeeded among failures, so there WAS something to cite. Not case A.

    This trace also guards the excerpt itself. An errored step's ``detail`` is the connector's own
    diagnostic, which is what the excerpt ruling covers. An OK step's ``detail`` is the opposite
    thing — the tool's RESULT: retrieved document text, search hits, a customer's rows. That must
    never reach the run page. An implementation reaching for "the last step's detail" rather than
    "the last ERRORED step's detail" would leak it, and would pass every other test here.
    """
    from oraclous_ohm.envelope import validate_grounding

    ok_with_payload = [
        {
            "index": 4,
            "kind": "tool",
            "name": "github.list_commits",
            "status": "ok",
            "tool_call_id": "tc-ok",
            "detail": '{"rows":[{"customer":"RETRIEVED-PAYLOAD"}]}',
        }
    ]
    message = " ".join(validate_grounding([], _errored_steps(3) + ok_with_payload))
    assert _OLD_ACCUSATION in message
    assert "RETRIEVED-PAYLOAD" not in message, "an OK step's result payload must never be excerpted"


def test_llm_steps_do_not_count_as_attempted_tool_calls() -> None:
    """A trace of pure LLM turns is case B — the member never called a tool at all."""
    from oraclous_ohm.envelope import validate_grounding

    llm_only = [{"index": 1, "kind": "llm", "name": "gpt", "status": "answer"}]
    assert " ".join(validate_grounding([], llm_only)) == " ".join(validate_grounding([], []))


async def test_the_run_page_message_names_the_real_cause() -> None:
    """End to end through run_team — this is the string the operator actually reads.

    Reproduces run eb08c17d: four failed searches, an honest 'I reached nothing' answer.
    """
    steps = _errored_steps(4, detail='{"error":"the web-search credential has no remaining quota"}')
    res = await run_team(
        _team([_m("researcher", tools=["web"])]),
        _dispatch_returning(
            {
                "output": "I could not access any sources — every search failed.",
                "status": "SUCCEEDED",
                "steps": steps,
            }
        ),
    )
    assert res.member_status == {"researcher": "failed"}  # fail-closed, unchanged
    message = res.member_errors["researcher"]
    assert _OLD_ACCUSATION not in message
    assert "quota" in message.lower()


# --- #696: a member with no tools is graded on the CLAIMS it makes, not on the tools it lacks ----
#
# Run fe548aac: the ``Adversarial-reviewer`` declared zero tools, made one LLM turn and no tool
# call, and closed with "All insights have been documented in two primary files:
# Interrail_B.V./Identified_Weaknesses_Support_Areas.txt ...". Neither file existed — it had no
# tool to write one — and it was graded succeeded. Its prose was the whole input of the Editor,
# which chased both paths for 10 rounds, 27 failed calls and 34,855 tokens, and failed the run.
#
# The contract these tests pin (RED until the [impl] lands):
#
# * ``validate_no_tool_claims(output, *, handed="")`` (new, ``oraclous_ohm.envelope``) — the pure
#   check for a ZERO-TOOL member's result. ``output`` is the member's result (a dict carrying its
#   ``output`` prose and, when it declared one, ``artifact_refs``; or a bare string). ``handed``
#   is everything the member was given — its inbound hand-off payloads and its objective — so a
#   location it repeats from there is not a claim. Errors (empty list = an honest reasoner):
#     - a non-empty ``artifact_refs`` (it persisted nothing: it has no tool that persists);
#     - a location in its prose that nobody handed it — a ``sandbox:`` reference or a path-like
#       token with a file extension — which is the exact shape of the fe548aac fabrication.
#   Messages fit the #685 budget (≤ 280 chars, the ``grounding: `` prefix brings it under 300).
# * ``run_team`` applies it to a member with EMPTY ``tools`` that dispatched successfully:
#   ``failed`` + ``member_errors[role]`` starting ``grounding:``, the same terminal the tool path
#   uses, so dependents are ``blocked`` by the ordinary upstream rule and the fabrication never
#   becomes a consumer's ground truth. An honest reasoner is byte-for-byte unchanged
#   (``test_zero_tool_member_keeps_prior_status_semantics`` above still holds), and a zero-tool
#   member still contributes NO ``member_grounding`` bucket either way.
#
# How the two are told apart (the "do not swing too far" decision): a reasoner that only talks
# about what it was handed is legitimate; a member that asserts a RESULT only a tool can produce
# — a persisted artifact, a location it invented — is not. The grade looks at the claim, never
# at the absence of tools.
#
# What "handed" means, pinned below: the member's own objective (subgoal / hand-off objective),
# every inbound hand-off payload, the run's inputs (the user's task text, fan-out state) and the
# fan-out item it was dispatched over. NOT handed: the member's own prompt body — a persona that
# instructs the member to name a file has not given it one (the byom e2e depends on this).
#
# Edges, pinned below: a URL is a reference, not an artifact the member persisted — never a
# claim. A bare filename with no directory IS a claim (the cheapest evasion of a
# directory-only rule). A dotted company name ("Interrail B.V.") and "e.g." are neither. The
# accepted false positive of a token rule: a reviewer that RECOMMENDS "add a CHANGELOG.md" is
# token-identical to one claiming it wrote one and will fail — that is the price of the bare-
# filename pin, and an impl must not "fix" it by weakening that pin. The issue's fuller claim
# taxonomy ("wrote to the graph", "fetched a page") is NOT pinned; the narrower
# artifact-location variant the issue allows is.

_FABRICATED_A = "Interrail_B.V./Identified_Weaknesses_Support_Areas.txt"
_FABRICATED_B = "Interrail_B.V./Consequences_of_Inaction_Summary.txt"
_FE548AAC_CLOSING = (
    "All insights have been documented systematically in two primary files for the team's "
    f"review and development:\n1. `{_FABRICATED_A}`\n2. `{_FABRICATED_B}`"
)


def test_validate_no_tool_claims_accepts_an_honest_reasoner() -> None:
    from oraclous_ohm.envelope import validate_no_tool_claims

    out = {
        "output": "Three weaknesses stand out at Interrail B.V.: pricing (v2.1 of the deck), "
        "support hours, e.g. weekends, and the 3.5% churn. Cf. the public figures at "
        "https://example.com/reports/2026/q1.pdf and on interrail.eu/support.",
        "status": "SUCCEEDED",
        "summary": "three weaknesses",
        "artifact_refs": [],
    }
    assert validate_no_tool_claims(out) == []


def test_validate_no_tool_claims_treats_a_url_as_a_reference_not_a_claim() -> None:
    """A reasoner citing a public source it was not handed is referencing, not persisting."""
    from oraclous_ohm.envelope import validate_no_tool_claims

    assert validate_no_tool_claims("See https://example.com/docs/report.pdf for the figures.") == []


def test_validate_no_tool_claims_rejects_a_bare_filename_it_was_not_handed() -> None:
    """The cheapest evasion of a directory-only rule: "saved as findings.md"."""
    from oraclous_ohm.envelope import validate_no_tool_claims

    errors = validate_no_tool_claims("I have saved all findings as findings.md for the team.")
    assert errors
    assert "findings.md" in errors[0]


def test_validate_no_tool_claims_rejects_a_non_empty_artifact_refs() -> None:
    from oraclous_ohm.envelope import validate_no_tool_claims

    out = {"output": "done", "artifact_refs": ["doc-1234"]}
    errors = validate_no_tool_claims(out)
    assert len(errors) == 1
    assert "artifact_refs" in errors[0]


def test_validate_no_tool_claims_rejects_the_fe548aac_paths() -> None:
    from oraclous_ohm.envelope import validate_no_tool_claims

    errors = validate_no_tool_claims({"output": _FE548AAC_CLOSING})
    assert errors, "two invented file paths must be a claim"
    assert _FABRICATED_A in errors[0]  # the FIRST invented location is named, so the row reads
    assert all(len(e) <= 280 for e in errors)  # #685 budget: with the prefix, under 300


def test_validate_no_tool_claims_rejects_a_sandbox_link() -> None:
    from oraclous_ohm.envelope import validate_no_tool_claims

    errors = validate_no_tool_claims("Saved the report to sandbox:/reports/findings.md for you.")
    assert errors
    assert "sandbox:/reports/findings.md" in errors[0]


def test_validate_no_tool_claims_accepts_a_location_the_member_was_handed() -> None:
    """A reasoner that repeats a path it was GIVEN is talking about its input, not claiming work."""
    from oraclous_ohm.envelope import validate_no_tool_claims

    handed = 'From reader: {"summary": "the brief is at docs/brief.md", "artifact_refs": []}'
    out = {"output": "Reading docs/brief.md, the brief asks for three things.", "artifact_refs": []}
    assert validate_no_tool_claims(out, handed=handed) == []


def test_validate_no_tool_claims_bounds_its_message() -> None:
    from oraclous_ohm.envelope import validate_no_tool_claims

    long_path = "reports/" + "a" * 500 + ".txt"
    errors = validate_no_tool_claims({"output": f"I wrote {long_path} for the team."})
    assert errors
    assert all(len(e) <= 280 for e in errors)


async def test_regression_run_fe548aac_zero_tool_reviewer_that_names_files_fails() -> None:
    # The reviewer holds no tools, so its "two primary files" can only be invented. It used to
    # pass — and its prose was the Editor's entire input. Now the reviewer FAILS on its own row
    # and the Editor is BLOCKED (never dispatched), instead of chasing files nobody wrote.
    dispatched: list[str] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        dispatched.append(member.role)
        return {"output": _FE548AAC_CLOSING, "status": "SUCCEEDED"}

    res = await run_team(
        _team([_m("reviewer"), _m("editor", ["reviewer"], tools=["edit"])]), dispatch
    )
    assert res.member_status == {"reviewer": "failed", "editor": "blocked"}
    assert res.status == "failed"
    assert dispatched == ["reviewer"]  # the Editor never burned a token on the invented paths
    message = res.member_errors["reviewer"]
    assert message.startswith("grounding:")
    assert _FABRICATED_A in message
    assert len(message) <= 300  # the #685 run-page budget holds for this message too


async def test_zero_tool_member_claiming_a_persisted_artifact_fails() -> None:
    # #697 made `artifact_refs` a declared key: "WHERE you persisted anything". A member with no
    # tool that persists has nothing to put there; a non-empty list is a claim without a receipt.
    res = await run_team(
        _team([_m("thinker")]),
        _dispatch_returning(
            {
                "output": '{"summary": "done", "artifact_refs": ["sandbox:/out/plan.md"]}',
                "status": "SUCCEEDED",
                "summary": "done",
                "artifact_refs": ["sandbox:/out/plan.md"],
            }
        ),
    )
    assert res.member_status == {"thinker": "failed"}
    assert res.member_errors["thinker"].startswith("grounding:")


async def test_zero_tool_member_repeating_a_handed_path_succeeds() -> None:
    # The reader is TOLD where the brief is (its subgoal); the thinker is HANDED that summary. Both
    # mention `docs/brief.md`, neither claims to have written it — both are legitimate reasoners.
    reader = OHMMember(
        role="reader",
        kind="agent",
        manifest_ref="org:x/reader@1",
        subgoal="say what the brief at docs/brief.md asks for",
        outputs_schema={"required": ["summary"]},
    )
    thinker = _m("thinker", ["reader"])
    outputs = {
        "reader": {
            "output": "docs/brief.md asks for a pricing review.",
            "status": "SUCCEEDED",
            "summary": "docs/brief.md asks for a pricing review",
            "artifact_refs": [],
        },
        "thinker": {
            "output": "Given docs/brief.md asks for a pricing review, start with the deck.",
            "status": "SUCCEEDED",
        },
    }

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return dict(outputs[member.role])

    res = await run_team(_team([reader, thinker]), dispatch)
    assert res.member_status == {"reader": "succeeded", "thinker": "succeeded"}
    assert res.status == "completed"


async def test_zero_tool_member_echoing_its_fan_out_item_succeeds() -> None:
    # A fan-out member is dispatched once per item; the item IS its input (state → over → item),
    # so echoing the item's path is reasoning over what it was handed.
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"output": f"{item} covers pricing.", "status": "SUCCEEDED"}

    fan = OHMFanOut(over="$.files", max_parallel=2)
    member = OHMMember(role="reader", kind="agent", manifest_ref="org:x/reader@1", fan_out=fan)
    res = await run_team(_team([member]), dispatch, state={"files": ["docs/a.md", "docs/b.md"]})
    assert res.member_status == {"reader": "succeeded"}


async def test_a_partial_zero_tool_member_is_graded_on_its_claims_too() -> None:
    # #587: a degraded (PARTIAL) member is still graded — a fabricated location fails it.
    res = await run_team(
        _team([_m("reviewer")]),
        _dispatch_returning({"output": _FE548AAC_CLOSING, "status": "PARTIAL"}),
    )
    assert res.member_status == {"reviewer": "failed"}
    assert res.member_errors["reviewer"].startswith("grounding:")


async def test_zero_tool_member_still_contributes_no_grounding_bucket() -> None:
    # The grounding SCORE stays a tool-declaring measure: a zero-tool member that fails this check
    # is reported through member_status/member_errors, never through member_grounding.
    res = await run_team(
        _team([_m("reviewer")]),
        _dispatch_returning({"output": _FE548AAC_CLOSING, "status": "SUCCEEDED"}),
    )
    assert res.member_status == {"reviewer": "failed"}
    assert res.member_grounding == {}
