"""#697 — a declared output contract must actually reach the hand-off, or it enforces nothing.

``outputs_schema`` is checked against the hand-off PAYLOAD. Today the payload a producer hands on is
the dispatch envelope — ``{"output": <the member's text>, "status", "steps", "driving_signals"}`` —
so a member that declares ``{"required": ["summary"]}`` can never satisfy it: its answer is prose
under the key ``output``, and ``summary`` is nowhere. Filling the declaration (the compiler half of
this issue) without this half would break every compiled team instead of fixing one.

So the member's own structured answer contributes its declared keys to the payload, put there by the
runtime rather than narrated by the model. That is what makes a consumer able to read
``payload["summary"]`` instead of parsing an essay.

Live evidence, team run ``50da3e09`` (2026-08-29, real model, through the gateway): the ``Reviewer``
succeeded and wrote its review as prose; the ``Publisher``, which depended on it, went looking for
"the review content" in the shared knowledge graph, failed both queries, and finished with nothing.
It had the review in hand and did not know it.

RED until the [impl] merges its declared keys into the payload.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from oraclous_execution_engine_service.services.team_run import run_team_harness
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


class _ScriptedHarness:
    """Answers each member with the text it was scripted to give; records what it was sent."""

    def __init__(self, answers: dict[str, str]) -> None:
        self._answers = answers
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        ref = str(kwargs.get("manifest_ref") or "")
        role = ref.split("/")[-1].split("@")[0]
        self.calls.append({"role": role, **kwargs})
        return {
            "id": str(uuid.uuid4()),
            "status": "SUCCEEDED",
            "output": self._answers.get(role, "ok"),
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


def _call(harness: _ScriptedHarness, role: str) -> dict[str, Any]:
    return next(c for c in harness.calls if c["role"] == role)


async def test_a_declared_key_reaches_the_result_the_consumer_reads() -> None:
    answer = json.dumps({"summary": "two blocking defects", "artifact_refs": ["doc:review-1"]})
    harness = _ScriptedHarness({"reviewer": answer})
    team = _team(
        [
            _m("reviewer", outputs_schema={"required": ["summary", "artifact_refs"]}),
            _m("publisher", depends_on=["reviewer"]),
        ]
    )
    res = await run_team_harness(team, harness)

    assert res.results["reviewer"]["summary"] == "two blocking defects"
    assert res.results["reviewer"]["artifact_refs"] == ["doc:review-1"]
    assert res.member_status == {"reviewer": "succeeded", "publisher": "succeeded"}


async def test_the_consumers_input_carries_the_named_keys_not_only_prose() -> None:
    # The point of the contract: the consumer reaches its producer's result BY NAME. If the keys
    # never render into what the consumer is sent, the declaration is decoration.
    answer = json.dumps({"summary": "two blocking defects", "artifact_refs": ["doc:review-1"]})
    harness = _ScriptedHarness({"reviewer": answer})
    team = _team(
        [
            _m("reviewer", outputs_schema={"required": ["summary", "artifact_refs"]}),
            _m("publisher", depends_on=["reviewer"]),
        ]
    )
    await run_team_harness(team, harness)

    sent = _call(harness, "publisher")["input_text"]
    assert "artifact_refs" in sent and "doc:review-1" in sent


async def test_a_declared_contract_asks_the_harness_for_a_parseable_answer() -> None:
    # A member that must return named keys must return something that PARSES. #853 already buys one
    # bounded repair turn for that; a declared contract is exactly when to spend it.
    harness = _ScriptedHarness({"reviewer": json.dumps({"summary": "s"})})
    team = _team([_m("reviewer", outputs_schema={"required": ["summary"]})])
    await run_team_harness(team, harness)
    assert _call(harness, "reviewer").get("requires_valid_json") is True


async def test_a_member_that_declares_nothing_is_unchanged() -> None:
    # Back-compat: every team compiled before this change declares nothing and must run as before,
    # with its prose reaching the consumer under `output`.
    harness = _ScriptedHarness({"a": "just prose"})
    team = _team([_m("a"), _m("b", depends_on=["a"])])
    res = await run_team_harness(team, harness)
    assert res.results["a"]["output"] == "just prose"
    assert res.member_status == {"a": "succeeded", "b": "succeeded"}
    assert _call(harness, "a").get("requires_valid_json") in (None, False)


async def test_a_producer_that_omits_a_declared_key_fails_on_its_own_row() -> None:
    # The ruling: "a producer that omits a declared key fails at its OWN hand-off, not at the
    # consumer." On run fe548aac the Editor paid 34,855 tokens for a reviewer's omission.
    harness = _ScriptedHarness({"reviewer": json.dumps({"notes": "no summary here"})})
    team = _team(
        [
            _m("reviewer", outputs_schema={"required": ["summary"]}),
            _m("publisher", depends_on=["reviewer"]),
        ]
    )
    res = await run_team_harness(team, harness)

    assert res.member_status["reviewer"] == "failed"
    assert res.member_status["publisher"] == "blocked"
    assert res.status == "failed"


async def test_a_member_is_told_which_keys_it_declared() -> None:
    """Enforcing a promise the member never heard is worse than not enforcing it.

    Run `b3fce78f` (2026-08-30, real model, through the gateway): the reviewer read the pull
    request, wrote a real review, and FAILED its own contract — nothing in its input had said the
    answer must carry `summary` and `artifact_refs`. Before the contract existed that member
    succeeded; the half-built version turned a working member into a failing one.
    """
    harness = _ScriptedHarness({"reviewer": json.dumps({"summary": "s", "artifact_refs": []})})
    team = _team([_m("reviewer", outputs_schema={"required": ["summary", "artifact_refs"]})])
    await run_team_harness(team, harness)

    sent = _call(harness, "reviewer")["input_text"]
    assert "summary" in sent and "artifact_refs" in sent
    assert "JSON object" in sent


async def test_a_member_that_declared_nothing_is_told_nothing() -> None:
    # Back-compat: an undeclared member's input is unchanged, so a pre-#697 team runs as before.
    harness = _ScriptedHarness({"a": "prose"})
    await run_team_harness(_team([_m("a")]), harness)
    assert "JSON object carrying exactly these keys" not in _call(harness, "a")["input_text"]
