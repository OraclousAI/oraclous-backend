"""#911 nightly real-model regression guard: a real model must no longer INVENT a source outside
``core/rest-connector@1.0.0``'s declared ``source_id`` enum once the fix lands — retargeted from an
earlier ``manifest-validate`` draft of this guard after a deployed-stack failure hunt found a
cleaner, reproducible real-model failure here instead (full evidence: issue #911, the comment
following #1041's Tests Review round). This is a narrower claim than "the call succeeds" — see the
test's own docstring and the OUT OF SCOPE section below for exactly where the line is drawn.

CAUSE, PRECISELY: this is about the dropped ALLOWED-VALUES list, not the dropped ``required`` list.
``core/rest-connector@1.0.0``'s ``fetch`` operation declares (``domain/plugins/builtin.py:800-810``)
``source_id`` constrained to exactly two values, ``alternative_me`` and ``mempool`` (via
``enum: available_sources()``), with valid endpoint keys ``tip_height`` (mempool.space's chain tip)
and ``fear_greed`` (alternative.me's Fear & Greed index). The model supplied BOTH arguments on every
call — this is not a case of a missing/optional argument — but today's hint-map-only schema hands
it a bare ``{"type": "string"}`` for ``source_id`` with no enum, so it cannot know which values are
legal and invents one.

DEPLOYED-STACK EVIDENCE (3 real runs, ``openrouter/openai/gpt-4o-mini``, 3/3 reproduced): a
one-member team bound only to ``rest-connector``, asked for Bitcoin chain data, invented
``source_id="blockchain_info"`` on EVERY call — 12, 11 and 9 calls across the three runs — burning
its whole tool budget on a source that does not exist. The verbatim failure each call reproduces:
``{"error": "RegistryError", "detail": "tool execution failed: unknown source 'blockchain_info'"}``.
After the #911 projection lands, the model sees the real ``enum`` and can supply a legal value.

WHY THE EXECUTION READ-BACK, NOT THE STEP TRACE: the run's ``results[<role>]["steps"]`` trace came
back EMPTY in the failure-hunt runs even for a member carrying no output contract — so, unlike the
earlier ``manifest-validate`` draft of this guard, the step trace is not a reliable read here. This
also sidesteps #1042 (a member that fails its declared output contract has its whole step-trace
record deleted) by declaring NO ``outputs_schema``/body-contract on the member at all. Instead:
``GET /api/v1/instances`` to find the auto-minted tool instance's ``last_execution_id``, then
``GET /api/v1/executions/{id}`` — both real gateway routes — asserting the execution's
``error_type`` is not ``UNKNOWN_SOURCE`` (deliberately NOT that its ``status`` is not ``FAILED`` —
see the test's own docstring: ``endpoint`` carries no allowed-values list either, so a real model
can still fail the call a different way, #1046, without the #911 bug having recurred). No manual
instance-connect step is needed first: ``rest-connector`` is keyless
(``CREDENTIAL_REQUIREMENTS`` is empty), so the harness's find-or-create (``_materialise``)
auto-mints an instance on first dispatch — the same
posture the existing ``core/knowledge-retriever`` in-loop test relies on
(``test_team_run_graph_retrieval_byom_gateway_e2e.py``, also keyless, also no pre-connect step).

OUT OF SCOPE, DELIBERATELY: the refusal message itself does not name the legal sources, which is
why the model kept repeating the same wrong guess — filed separately as #1044. Fixing both at once
here would make this test's before/after proof ambiguous, so nothing in this file asserts on the
wording of the refusal. Also out of scope: ``endpoint``'s legal values depend on which
``source_id`` was chosen, so the declared schema cannot carry a static allowed-values list for it
either — a real model can still choose a legal ``source_id`` and then a wrong ``endpoint`` for it
(observed live: ``source_id="alternative_me"`` with the wrong ``endpoint``, failing
``INVALID_INPUT``, not ``UNKNOWN_SOURCE``). That is a separate, real gap, filed as #1046. This
test's only claim is that the model no longer invents a source outside the declared set.

Model non-determinism: pinned to the one model that reproduced the failure
(``openrouter/openai/gpt-4o-mini``), and only the outcome of the call is asserted — never an exact
call count, since a real model may retry a different number of times run to run.

Requires (same as the other BYOM tests):
  - the harness in LIVE mode  (HARNESS_LLM_MODE=live — ``scripts/e2e.sh --byom``)
  - OPENROUTER_API_KEY in the env (the user's BYOM key)
Skipped otherwise, so it never reddens the deterministic suite or unit CI.

Marker: ``byom`` only (the full nightly real-model leg), deliberately NOT ``byom_smoke`` — same
posture as the guard this replaces (``tests/e2e/README.md``'s per-PR subset stays a fixed ~5 tests).
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_USER_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")  # the user's own key, provided via env
requires_byom_key = pytest.mark.skipif(
    _USER_MODEL_KEY is None, reason="OPENROUTER_API_KEY not set (BYOM real-LLM run)"
)

#: the model that reproduced the failure 3/3 times on the deployed stack during the failure hunt —
#: pinned deliberately, NOT the suite's usual ``E2E_MODEL`` default, so this guard keeps testing the
#: exact surface that failed rather than whatever the default happens to be today.
_MODEL_BINDING = "openrouter/openai/gpt-4o-mini"


def _byom_model(credential_id: str) -> dict:
    return {
        "role": "primary",
        "binding": _MODEL_BINDING,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
    }


def _fetcher_studio(root: Path) -> None:
    """A one-member studio bound only to ``rest-connector`` (a raw tool name, not a Claude Code
    builtin — the importer maps ANY declared tool name onto ``core/<slug>@<version>``, no
    allowlist: ``packages/ohm/src/oraclous_ohm/import_/mapping.py``'s ``_capability_ref``).
    Deliberately declares no output contract (see the module docstring, #1042)."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    body = (
        "Use your rest-connector tool to fetch current Bitcoin chain data: the chain tip height "
        "and the Fear & Greed index. Then report what you found."
    )
    (agents / "fetcher.md").write_text(
        f"---\nname: fetcher\nmodel: sonnet\ntools: rest-connector\n---\n{body}\n"
    )
    (root / "teams" / "1-fetch").mkdir(parents=True)
    (root / "teams" / "1-fetch" / "charter.md").write_text(
        "# Team I — Fetch\n## Roster\n| Agent | Type | Model | Job |\n"
        "| --- | --- | --- | --- |\n| `fetcher` | subagent | sonnet | fetch chain data |\n"
    )


def _poll(client: httpx.Client, run_id: str, until: set[str], tries: int = 40) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = client.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in until:
            return row
        time.sleep(2)
    raise AssertionError(f"run {run_id} never reached {until} (last: {row.get('state')})")


@requires_byom_key
@pytest.mark.byom
def test_a_real_models_rest_connector_call_does_not_invent_an_unknown_source(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """#911 regression guard — PROVES ONE THING ONLY: the real model does not invent a source
    outside the declared ``source_id`` enum (the deployed-stack failure this guards against: 3/3
    real runs on ``openrouter/openai/gpt-4o-mini`` invented ``source_id="blockchain_info"`` on
    every call, an error_type of ``UNKNOWN_SOURCE``). That is the #911 claim, precisely: the
    projection lands the ``source_id`` enum, and the model uses it.

    DELIBERATELY DOES NOT PROVE the model gets the whole call right. ``endpoint``'s legal values
    depend on which ``source_id`` was chosen (``mempool`` -> ``tip_height``, ``alternative_me`` ->
    ``fear_greed``), and that dependency has no static allowed-values list for the projection to
    carry — the declared schema can only say ``{"type": "string", "minLength": 1}`` for it. The
    model still has to guess ``endpoint`` and sometimes guesses wrong (observed live: a LEGAL
    ``source_id`` of ``alternative_me`` paired with the wrong ``endpoint``, failing with
    ``error_type: "INVALID_INPUT"``, ``error_message: "'endpoint' must be one of ['fear_greed']"``)
    — a separate, real gap this test does not assert on and is not the #911 fix's job to close.
    Filed as #1046 ("The data-source reader's endpoint argument has no declared legal values, so
    a model guesses it").

    Deliberately does NOT read ``results[<role>]["steps"]`` — that trace came back empty for this
    member in every failure-hunt run — and instead reads the auto-minted tool instance's
    ``last_execution_id`` and the execution record behind it, both real gateway routes.
    Deliberately does NOT assert on the refusal message's wording (#1044 is the separate, in-scope
    follow-up for that). Deliberately does NOT assert the run's overall terminal ``state`` or the
    execution's overall ``status`` either — only ``error_type != "UNKNOWN_SOURCE"``, which isolates
    the ONE claim #911 actually makes from the separate ``endpoint``-guessing gap above."""
    user = register(f"restconn{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])

    # 1) the user stores THEIR OWN model token via the real credential API
    cred = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "my openrouter model",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _USER_MODEL_KEY},
        },
    )
    assert cred.status_code == 201, cred.text
    credential_id = cred.json()["id"]

    # 2) import the one-member rest-connector studio; point it at the user's BYOM model
    _fetcher_studio(tmp_path)
    imported = import_setup(tmp_path, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    sub_harnesses = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    assert set(sub_harnesses) == {"fetcher"}
    caps = {cp["binding"]: cp["ref"] for cp in sub_harnesses["fetcher"]["capabilities"]}
    assert "rest-connector" in caps, caps
    for sub in sub_harnesses.values():
        sub["models"] = [_byom_model(credential_id)]

    # 3) run the team THROUGH THE GATEWAY — real engine -> worker -> live harness -> real OpenRouter
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": imported.manifest.model_dump(mode="json"),
            "sub_harnesses": sub_harnesses,
            "gate_decisions": {},
        },
    )
    assert created.status_code == 202, created.text
    # terminal state only, never asserted on below (see the docstring: a failure elsewhere in the
    # run must not mask this test's real subject, the tool call's own outcome)
    _poll(c, created.json()["id"], {"SUCCEEDED", "FAILED", "REJECTED"})

    # 4) find the auto-minted rest-connector instance and read its last execution back. No
    # manual connect step created it (see the module docstring) — `_materialise`'s fresh-mint
    # branch mints it the first time the harness dispatches the tool, keyed to this run's
    # capability id, so it is found by capability id rather than a guessed instance name.
    cap_rows = c.get("/api/v1/capabilities", params={"kind": "tool"}).json()["capabilities"]
    rest_connector_cap = next((r for r in cap_rows if r["name"] == "REST Connector"), None)
    assert rest_connector_cap is not None, "REST Connector is not a registered capability"

    instance_rows = c.get("/api/v1/instances").json()["instances"]
    matching = [
        r for r in instance_rows if str(r.get("capability_id")) == str(rest_connector_cap["id"])
    ]
    assert matching, (
        f"no rest-connector instance exists for this organisation after the run — the member "
        f"never dispatched the tool at all. This is a hard failure of the #911 guard, not a "
        f"skip. instances={instance_rows}"
    )
    execution_id = matching[0].get("last_execution_id")
    assert execution_id, f"the rest-connector instance recorded no execution: {matching[0]}"

    execution = c.get(f"/api/v1/executions/{execution_id}")
    assert execution.status_code == 200, execution.text
    body = execution.json()
    assert body.get("error_type") != "UNKNOWN_SOURCE", (
        f"the real model invented a source outside the declared enum (today's reproduced bug: "
        f"{{'error': 'RegistryError', 'detail': \"tool execution failed: unknown source "
        f"'blockchain_info'\"}} — the model guesses because the schema carries no enum for "
        f"source_id). execution={body}"
    )
