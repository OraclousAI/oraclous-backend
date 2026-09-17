"""#1111 item 3 — a search tool whose credential the provider refuses fails FAST, through the
gateway (:8006) — NO fakes.

The reported shape (#1111): a member whose search-tool credential the provider will not honour
used to come back as a burst of errored tool calls (the harness's own dispatch() drops the
registry's curated ``error_type`` and raises a bare ``RegistryError``, so every tool failure goes
back to the model as anonymous prose and the model keeps trying). The ruling (item 2/3): a 401/403
from the registry's own ``classify_provider_status`` is NOT transient — the member fails on its
FIRST refused call, with a curated token (``tool_credential_rejected``, parallel to #1108's
``llm_credential_rejected``) naming the cause, never a retry burst.

Credential variant chosen, and why: a genuinely spent search-provider quota (the OTHER curated
token, ``tool_quota_exhausted``) cannot be reproduced on demand — it depends on an external
account's real usage history, not anything this test controls. A deliberately WRONG credential is
reproducible on demand and gets the same provider-refusal family (401/403 -> ``PROVIDER_AUTH_
FAILED``, ``search_providers.classify_provider_status``), so this test pins the credential-rejected
half of item 3 and leaves the quota half undemonstrated at the e2e layer (unit-level only, per the
#1111 scoping notes) — a real gap, not an oversight: there is no public endpoint that lets a user
exhaust a provider's quota on request.

A real user, through the gateway, registers, brings their OWN OpenRouter key (BYOM, real model —
a fake-mode run cannot exercise the loop's tool-call decision at all, CLAUDE.md rule 8) and a
DELIBERATELY WRONG Tavily-shaped key via the real credentials API, runs a one-member team whose
member must search before answering, and reads the outcome back through two public reads: the
team run's own state, and the member's own harness execution (steps + error_type + error_message) —
never the database, never a service port, never `/internal` (FUCK_CLAUDE_FUCK_PAPERCLIP rule 5).

Auto-skips when the gateway is down (conftest) or ``OPENROUTER_API_KEY`` is unset — a skip is NOT a
pass (rule 3). No real Tavily key is needed: the whole point is a key the provider refuses, and
Tavily's real API refuses ANY malformed/unknown key with a real 401 — no third party is faked.

RED until the impl lands: today the harness's error_type classifier has no ``tool_credential_
rejected`` token at all (the harness-side gap #1111 items 2/3 exist to close), so the strict
``error_type`` equality below cannot pass, whatever a live model happens to do with the bare error
text it is handed instead.
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
_POLL_BUDGET_SECONDS = 180.0

#: The curated token item 3 mints, parallel to #1108's ``llm_credential_rejected`` — a member
#: error_type, not a top-level error-taxonomy code (it is not promoted through the gateway error
#: wall the way ``MODEL_CREDENTIAL_REJECTED`` is; #1111's scoping found no such need for it).
_TOKEN = "tool_credential_rejected"  # noqa: S105 — an error_type token, not a secret


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


def _connect_web_research(c: httpx.Client, credential_id: str) -> None:
    """Give this org a configured Web Research instance — the same org-level resolution a team-run
    member's ``tools: ["web-research"]`` dispatch reads at run time, through the real public API."""
    catalogue = c.get("/api/v1/capabilities").json()["capabilities"]
    capability = next((x for x in catalogue if x["name"] == "Web Research"), None)
    assert capability is not None, "the registry has no Web Research capability"
    instance = c.post(
        "/api/v1/instances",
        json={"capability_id": capability["id"], "name": "tool-cred-rejected", "configuration": {}},
    )
    assert instance.status_code == 201, instance.text
    configured = c.post(
        f"/api/v1/instances/{instance.json()['id']}/configure-credentials",
        json={"credential_mappings": {"api_key": credential_id}},
    )
    assert configured.status_code == 200, configured.text


_SUBGOAL = (
    "You have exactly one tool: web search. Use it ONCE to search for the query "
    '"Eurail global pass benefits", then, whatever the tool returns, write in your `summary` '
    "either a one-sentence takeaway from the results, or (if the tool call did not succeed) that "
    "it did not succeed. Do not call the tool a second time."
)


def _single_member_team(org: str) -> dict:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "tool-credential-rejected-proof",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": [
            {
                "role": "searcher",
                "kind": "agent",
                "manifest_ref": "org:proof/searcher@1",
                "tools": ["web-research"],
                "tool_rationale": {"web-research": "it must search before it can answer"},
                "outputs_schema": {"required": ["summary"]},
                "subgoal": _SUBGOAL,
            }
        ],
        "runtime": {"entrypoint": "searcher"},
    }


def _searcher_sub(org: str, model_credential_id: str) -> dict:
    """The member's own single-agent sub-harness, built through the OHM library — as a client
    does, not hand-rolled here."""
    from oraclous_ohm.import_.mapping import build_subharness
    from oraclous_ohm.manifest import OHMModel

    sub = build_subharness(
        "searcher",
        owner_organization_id=uuid.UUID(org),
        body="You search the web and report what you found, plainly.",
        tools=["web-research"],
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


def _searcher_execution(c: httpx.Client, run_id: str) -> dict:
    """The searcher member's OWN harness execution, read through two public reads (the run-tree
    for the id, then the execution itself) — never the database, never a harness/engine port."""
    tree = c.get(f"/v1/engine/team-runs/{run_id}/tree")
    assert tree.status_code == 200, tree.text
    children = tree.json()["children"]
    match = next((child for child in children if child.get("role") == "searcher"), None)
    assert match is not None, f"no run-tree child recorded for role 'searcher' — {children}"
    execution = c.get(f"/v1/harnesses/executions/{match['execution_id']}")
    assert execution.status_code == 200, execution.text
    return dict(execution.json())


@requires_byom_key
def test_a_rejected_search_credential_fails_the_member_on_the_first_call(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF: one refused call, one curated token — never a burst of anonymous tool errors."""
    user = register(f"toolcred{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])

    model_credential = _store_credential(
        c, user["user_id"], "openrouter", str(_MODEL_KEY), "e2e model key"
    )
    # Deliberately WRONG: not this org's real key, not any key Tavily ever issued. Tavily's real
    # API refuses it with a genuine 401 — no third party is faked (rule 5), and no real
    # TAVILY_API_KEY is spent or even needed, unlike a search-happy-path proof.
    bogus_search_key = "tvly-" + uuid.uuid4().hex
    search_credential = _store_credential(
        c, user["user_id"], "tavily", bogus_search_key, "deliberately wrong tavily key"
    )
    _connect_web_research(c, search_credential)

    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": _single_member_team(user["org_id"]),
            "sub_harnesses": {"searcher": _searcher_sub(user["org_id"], model_credential)},
            "gate_decisions": {},
        },
    )
    assert created.status_code == 202, created.text
    assert bogus_search_key not in created.text

    done = _poll(c, created.json()["id"])
    assert bogus_search_key not in json.dumps(done), done
    assert search_credential not in json.dumps(done), done

    # The headline: a credential the provider refuses is a hard member failure, not something a
    # cooperative model gets to narrate its way past with a soft "the search didn't work" answer.
    assert done["state"] == "FAILED", (
        f"a rejected search credential must fail the run, not complete around it — {done}"
    )

    execution = _searcher_execution(c, created.json()["id"])
    dumped = json.dumps(execution)
    assert bogus_search_key not in dumped, "the wrong key must never leak through the run record"
    assert search_credential not in dumped, "the credential id must never leak through the record"

    # The curated token, visible via the public API — never a bare/anonymous exception shape.
    assert execution["error_type"] == _TOKEN, execution
    message = (execution.get("error_message") or "").lower()
    assert "credential" in message, execution

    # A real model ran this — the assertion above is not the fake harness papering over a call
    # that was never really refused by anyone.
    assert execution["simulated"] is False, execution

    # THE FAIL-FAST SHAPE: exactly one tool dispatch, not #1111's reported "14 calls, all errored".
    # The loop never gives the model a second turn on a credential rejection — this is a structural
    # guarantee of the fix, independent of whether a given model would have retried if it could.
    tool_steps = [s for s in execution["steps"] if s.get("kind") == "tool"]
    assert len(tool_steps) == 1, (
        f"expected exactly one tool dispatch (fail-fast on the first refused call), got "
        f"{len(tool_steps)} — {tool_steps}"
    )
    assert tool_steps[0]["status"] == "error", tool_steps[0]
