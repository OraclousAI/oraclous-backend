"""#1042 DEPLOYED-STACK proof through the API GATEWAY — a failed member's tool trace stays
readable — NO fakes.

Ruling (solution-architect, on #1042): the trace of a member that fails its own OUTPUT CONTRACT
(``outputs_schema.required``, the #697 rule) is never deleted. ``results[role]`` is set to
``None`` at the contract-failure branch (``packages/ohm/src/oraclous_ohm/orchestrate.py``), but
that member's own harness execution — the ``steps`` its tool-use loop recorded — is untouched and
was never copied into the team-run row in the first place (``_grade_grounding`` already strips
``steps`` off every stored member output, succeeded or not, per #642). The engine records
``(execution_id, role)`` through ``on_child`` before any status check, so a contract-failure child
is always on the run tree. The read path for a person holding only a gateway token is:
``GET /v1/engine/team-runs/{id}/tree`` -> the child whose ``role`` matches -> ``GET
/v1/harnesses/executions/{execution_id}`` -> ``.steps``. That is exactly the path
``test_tool_credential_rejected_fail_fast_gateway_e2e.py``'s ``_searcher_execution`` already uses
for a harness-level failure; this file pins the SAME path for a team-level contract failure.

A real user, through the gateway, registers, brings their OWN OpenRouter key (BYOM — a fake-mode
run cannot exercise a real model's tool-call/answer decision at all, CLAUDE.md rule 8), and starts
a ONE-member team run. The member is bound to the seeded, keyless ``math-tools`` group (#822, no
credential to configure) and declares an output contract requiring a ``summary`` key
(``outputs_schema={"required": ["summary"]}``). The member is told to call the tool TWICE, but its
own ``max_tool_calls`` is set to 1 with ``on_exhaustion="degrade"``: the FIRST call dispatches (a
real, readable tool step — this is what the trace check below reads), the model's SECOND attempt is
refused by the harness's own tool-call budget gate (never reached — real prompt content, never a
schema trick; a non-string ``required`` entry is forbidden by the issue's own scoping), and the loop
settles with no final answer at all, so ``summary`` is never delivered. This is a structural,
resource-budget outcome — not a bet on a live model choosing to disobey the platform's own output-
format directive (probed empirically: a real model reliably WRITES the declared JSON key once
told about it, so fighting that directly is not the reliable trigger). A live model could in
principle stop after the FIRST call instead of attempting the second (satisfying its own contract
after all) — a run that settles SUCCEEDED is retried (fresh team-run, same org/credential) up to
three times before the test fails outright — a skip is never a pass (rule 3).

Once a run settles on the contract failure, the proof: the run is FAILED, the member is "failed",
its result is None, and its error names the contract miss — AND its own harness execution is still
reachable through the tree and still carries the ``math-tools`` tool step, readable and intact.
Never the database, never a service port, never ``/internal`` (FUCK_CLAUDE_FUCK_PAPERCLIP.md rule
5). Auto-skips when the gateway is down (conftest) or ``OPENROUTER_API_KEY`` is unset — a skip is
NOT a pass (rule 3). Expected to PASS on ``main`` — the ruling says the read path already works;
a FAIL here for a reason other than model-compliance flake overturns that ruling.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom_key = pytest.mark.skipif(
    not _MODEL_KEY, reason="OPENROUTER_API_KEY not set (the user's BYOM real-model leg)"
)

_TERMINAL = {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED", "COST_BUDGET"}
_POLL_BUDGET_SECONDS = 240.0
#: A real model can decide to stop after the FIRST tool call and answer directly, satisfying its
#: own contract after all — a live-model outcome this test cannot force. Retried this many times
#: (fresh team-run, same org/credential) before the test fails outright rather than skip.
_MAX_CONTRACT_ATTEMPTS = 3

_ROLE = "solver"

_SUBGOAL = (
    "You have exactly one tool: math-tools. Call it TWICE, in this order: first the "
    "compound_growth operation with start=1000, rate=0.05, periods=4, then the "
    "percentage_change operation with start=1000, end=1500. Wait for each result before making "
    "the next call. After both calls, answer with a one-sentence summary of both results."
)


def _store_credential(c: httpx.Client, user_id: str, provider: str, key: str, name: str) -> str:
    resp = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": name,
            "provider": provider,
            "cred_type": "api_key",
            "credential": {"api_key": key},
        },
    )
    assert resp.status_code == 201, resp.text
    assert key not in resp.text, "the credential secret must never be echoed by the store response"
    return str(resp.json()["id"])


def _single_member_team(org: str) -> dict:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "contract-failed-member-trace-proof",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": [
            {
                "role": _ROLE,
                "kind": "agent",
                "manifest_ref": "org:proof/solver@1",
                "tools": ["math-tools"],
                "tool_rationale": {"math-tools": "it must compute before it can answer"},
                "outputs_schema": {"required": ["summary"]},
                "subgoal": _SUBGOAL,
                # The deterministic trigger: the member is asked for TWO calls but only gets ONE
                # (#1111's tool-call budget gate refuses the second), so the loop settles with no
                # final answer at all and the declared `summary` key is never delivered.
                "max_tool_calls": 1,
                "on_exhaustion": "degrade",
            }
        ],
        "runtime": {"entrypoint": _ROLE},
    }


def _member_sub(org: str, model_credential_id: str) -> dict:
    """The member's own single-agent sub-harness, built through the OHM library — as a client
    does, not hand-rolled here. ``math-tools`` is keyless (#822) — no credential to connect."""
    from oraclous_ohm.import_.mapping import build_subharness
    from oraclous_ohm.manifest import OHMModel

    sub = build_subharness(
        _ROLE,
        owner_organization_id=uuid.UUID(org),
        body="You compute with math-tools and report what you found, plainly.",
        tools=["math-tools"],
        model=OHMModel(
            role="primary",
            binding=os.environ["E2E_MODEL"],
            protocol_shape="openai-compatible",
            config={"credential_id": model_credential_id},
        ),
    )
    return sub.model_dump(mode="json")


def _poll(c: httpx.Client, run_id: str) -> dict:
    deadline = time.monotonic() + _POLL_BUDGET_SECONDS
    row: dict = {}
    while time.monotonic() < deadline:
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in _TERMINAL:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


def _member_execution(c: httpx.Client, run_id: str, role: str) -> dict:
    """This member's OWN harness execution, read through two public reads (the run-tree for the
    id, then the execution itself) — never the database, never a harness/engine port."""
    tree = c.get(f"/v1/engine/team-runs/{run_id}/tree")
    assert tree.status_code == 200, tree.text
    children = tree.json()["children"]
    match = next((child for child in children if child.get("role") == role), None)
    assert match is not None, f"no run-tree child recorded for role {role!r} — {children}"
    execution = c.get(f"/v1/harnesses/executions/{match['execution_id']}")
    assert execution.status_code == 200, execution.text
    return dict(execution.json())


@requires_byom_key
def test_a_member_that_fails_its_contract_keeps_its_tool_trace_readable(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF: a contract-failed member's tool step is still readable through the gateway."""
    user = register(f"contracttrace{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])

    model_credential = _store_credential(
        c, user["user_id"], "openrouter", str(_MODEL_KEY), "e2e model key"
    )

    done: dict = {}
    for attempt in range(1, _MAX_CONTRACT_ATTEMPTS + 1):
        created = c.post(
            "/v1/engine/team-runs",
            json={
                "manifest": _single_member_team(user["org_id"]),
                "sub_harnesses": {_ROLE: _member_sub(user["org_id"], model_credential)},
                "gate_decisions": {},
            },
        )
        assert created.status_code == 202, created.text

        done = _poll(c, created.json()["id"])
        if done["state"] == "FAILED" and done.get("member_status", {}).get(_ROLE) == "failed":
            break
        assert attempt < _MAX_CONTRACT_ATTEMPTS, (
            f"the model never attempted the second tool call on any of {_MAX_CONTRACT_ATTEMPTS} "
            f"fresh runs, so its tool-call budget was never exhausted and the contract it cannot "
            f"otherwise satisfy was met instead — last run: {done}"
        )

    run_id = done["id"]

    # THE HEADLINE: a member that fails its own output contract fails the run, not silently.
    assert done["state"] == "FAILED", done
    assert done["member_status"][_ROLE] == "failed", done
    assert done["results"][_ROLE] is None, done

    error_message = done.get("error_message") or ""
    assert "declared an output contract it did not deliver" in error_message, done

    # THE PROOF: the failed member's own execution is still on the run tree and still readable.
    execution = _member_execution(c, run_id, _ROLE)
    dumped = json.dumps(execution)
    assert model_credential not in dumped, "the credential id must never leak through the record"

    tool_steps = [s for s in execution["steps"] if s.get("kind") == "tool"]
    assert tool_steps, f"the failed member's trace carries no tool step at all — {execution}"
    assert any(step.get("name", "").startswith("math-tools.") for step in tool_steps), (
        f"no tool step names the math-tools group the member was bound to — {tool_steps}"
    )
