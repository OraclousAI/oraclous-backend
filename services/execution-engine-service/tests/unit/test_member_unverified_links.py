"""#944 — carrying "this member linked a page it never fetched" out to the reader.

The harness decides the verdict (its own tests pin that). This file pins the two engine steps that
get the verdict onto a screen, because a check nobody can see is the silent trust the issue is
about. The frontend now renders a member's inline ``[Source](url)`` as a real anchor, so a reader
clicks it believing the run fetched that page.

**The shape, RULED by the owner on #944 (2026-09-07)**, following the ``simulated`` precedent (#907)
exactly rather than inventing a third pattern:

* Each member's stored result gains ``unverified_links``: the URLs in that member's answer that the
  run never fetched. A list, empty when clean, never absent — a consumer reads it on every member,
  and a missing key would make "clean" indistinguishable from "not checked".
* ``TeamRunOut`` gains ``has_unverified_links``: true when ANY member's list is non-empty. Derived
  from ``results``, never stored, so it cannot disagree with the members it summarises. This is what
  a run-level warning banner reads; the per-member list is what a per-member panel reads.

**No new column and no migration**, on either side. ``results`` is already a JSON document, and the
run-level flag is derived at read time — the same reason ``partial`` and ``simulated`` are.

**Why a boolean at run level rather than a count or the merged list.** A count invites a reader to
treat "3 bad links" as three times worse than one, which it is not; one fabricated citation
discredits every other citation on the same answer equally. The merged list loses which member said
what, which is the only thing that makes it actionable.

RED until the ``[impl]`` lands: the key is not lifted and the field does not exist.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.schema.engine_schemas import TeamRunOut
from oraclous_execution_engine_service.services.team_run import run_team_harness
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_FABRICATED = "https://www.okta.com/blog/2023/10/okta-ai-token-costs"


class _ScriptedHarness:
    """Answers each member with its scripted text, and reports the unverified links the harness's
    own in-loop check found for it."""

    def __init__(self, answers: dict[str, str], unverified: dict[str, list[str]]) -> None:
        self._answers = answers
        self._unverified = unverified

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        ref = str(kwargs.get("manifest_ref") or "")
        role = ref.split("/")[-1].split("@")[0]
        return {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": self._answers.get(role, "ok"),
            "unverified_links": self._unverified.get(role, []),
        }


def _m(role: str, **over: Any) -> OHMMember:
    return OHMMember(role=role, kind="agent", manifest_ref=f"org:x/{role}@1", **over)


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


def _run_out(results: dict[str, Any]) -> TeamRunOut:
    return TeamRunOut(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        state="SUCCEEDED",
        results=results,
        paused_at=[],
        error_message=None,
        created_at=None,
    )


# --- the per-member key -----------------------------------------------------------------------


async def test_a_members_unverified_links_reach_its_stored_result() -> None:
    harness = _ScriptedHarness(
        {"linker": f"Costs fell. [Source]({_FABRICATED})"}, {"linker": [_FABRICATED]}
    )
    res = await run_team_harness(_team([_m("linker")]), harness)
    assert res.results["linker"]["unverified_links"] == [_FABRICATED]


async def test_a_clean_member_carries_an_empty_list_not_a_missing_key() -> None:
    harness = _ScriptedHarness({"linker": "Costs fell."}, {})
    res = await run_team_harness(_team([_m("linker")]), harness)
    assert res.results["linker"]["unverified_links"] == []


async def test_a_harness_response_predating_this_change_reports_clean() -> None:
    # Back-compat, the #907 posture: a response with no `unverified_links` key at all must not
    # crash the run and must not be reported as unverified.
    class _Old:
        async def execute(self, **kwargs: Any) -> dict[str, Any]:
            return {"id": str(uuid.uuid4()), "status": "SUCCEEDED", "output": "ok"}

    res = await run_team_harness(_team([_m("linker")]), _Old())
    assert res.results["linker"]["unverified_links"] == []


async def test_a_member_cannot_clear_its_own_flag_by_answering_with_the_key() -> None:
    # The envelope's own keys are platform-owned. A member that writes {"unverified_links": []}
    # into its answer must not overwrite the runtime's verdict about itself — that would hand the
    # model the off-switch for the check that watches it.
    harness = _ScriptedHarness(
        {"linker": '{"unverified_links": [], "summary": "all good"}'}, {"linker": [_FABRICATED]}
    )
    team = _team([_m("linker", outputs_schema={"required": ["unverified_links", "summary"]})])
    res = await run_team_harness(team, harness)
    assert res.results["linker"]["unverified_links"] == [_FABRICATED]


# --- the run-level flag -------------------------------------------------------------------------


def test_the_run_is_flagged_when_any_member_has_an_unverified_link() -> None:
    out = _run_out(
        {
            "researcher": {"output": "…", "unverified_links": []},
            "linker": {"output": "…", "unverified_links": [_FABRICATED]},
        }
    )
    assert out.has_unverified_links is True


def test_the_run_is_not_flagged_when_every_member_is_clean() -> None:
    out = _run_out(
        {
            "researcher": {"output": "…", "unverified_links": []},
            "linker": {"output": "…", "unverified_links": []},
        }
    )
    assert out.has_unverified_links is False


def test_a_blocked_members_null_result_does_not_crash_the_derivation() -> None:
    # A failed or blocked member is results[role]=None, never a dict — the guard `simulated`
    # already needs, and the shape that would 500 the whole run detail read without it.
    out = _run_out({"linker": None, "researcher": {"output": "…", "unverified_links": []}})
    assert out.has_unverified_links is False


def test_a_pre_change_run_with_no_key_reports_not_flagged() -> None:
    out = _run_out({"linker": {"output": "…"}})
    assert out.has_unverified_links is False
