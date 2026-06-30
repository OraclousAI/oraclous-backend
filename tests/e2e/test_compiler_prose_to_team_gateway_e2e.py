"""#594 — a prose objective COMPILES to a runnable Team Harness, proven on the LIVE stack (ADR-047).

The Harness Compiler is itself an OHM v1.1 Team Harness (planner → capability-surveyor →
manifest-drafter → reviewer). Given ONLY a prose objective + a seeded tool catalog, it runs through
the gateway — real registration → JWT → credential → engine → worker → LIVE harness → real
OpenRouter (no fakes) — and the reviewer emits a drafted team that the SAME #593 validator
(assemble_and_report) confirms is assemblable (would_block False): prose → runnable team. The
capability-absence GATE is proven DETERMINISTICALLY by invoking core/manifest-validate@1 directly on
the deployed registry (no LLM): a draft naming an unsurveyed tool → would_block True,
F-CAPABILITY-MISSING. Auto-skips without the BYOM key/gateway (a skip is NOT a pass).
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_MODEL = "openrouter/openai/gpt-4o-mini"

# a seeded catalog of REAL registered tools the drafter may use (slice-1; live survey = fast-follow)
_CATALOG = [
    {"name": "web-research", "ref": "core/web-research@1.0.0"},
    {"name": "send-to-drafts", "ref": "core/send-to-drafts@1.0.0"},
]
_OBJECTIVE = "Research this week's most-cited AI papers and compile a short plain-text digest."


def _model(cred_id: str) -> dict:
    return {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": cred_id},
    }


def _cred(c: httpx.Client, user: dict) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "byom",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _OR_KEY},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _bind(subs: dict, cred_id: str) -> dict:
    out = {role: dict(sub) for role, sub in subs.items()}
    for sub in out.values():
        sub["models"] = [_model(cred_id)]
    return out


def _poll(c: httpx.Client, run_id: str, tries: int = 160) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


@requires_byom
def test_a_prose_objective_compiles_to_a_runnable_team(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    from oraclous_ohm.compiler import build_compiler_team
    from oraclous_ohm.import_ import assemble_and_report
    from oraclous_ohm.manifest import OHMMember
    from oraclous_ohm.parse import load_ohm

    user = register(f"compiler{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])

    # the compiler team, seeded with the prose objective + the surveyed catalog (slice-1 baked)
    manifest, subs = build_compiler_team(org, objective=_OBJECTIVE, catalog=_CATALOG)
    doc = manifest.model_dump(mode="json")
    doc["models"] = [_model(cred)]
    gid = c.post("/api/v1/graphs", json={"name": "compiler-run"}).json()["id"]

    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": doc,
            "sub_harnesses": _bind(subs, cred),
            "gate_decisions": {},
            "graph_id": gid,
        },
    )
    assert created.status_code == 202, created.text
    row = _poll(c, created.json()["id"])
    assert row["state"] == "SUCCEEDED", f"the compiler team must run end-to-end — {row}"

    # the reviewer emitted the compiled team JSON (peeled from its text) — prose → a team manifest.
    # a member result is {"output": <text>, "status": ...}; older shapes return the text directly.
    raw = (row.get("results") or {}).get("reviewer")
    assert raw, f"the reviewer produced the compiled team — {row}"
    reviewer_text = raw["output"] if isinstance(raw, dict) else raw
    match = re.search(r"\{.*\}", reviewer_text, re.DOTALL)
    assert match, f"the reviewer's output carries a JSON team — {reviewer_text!r}"
    compiled = json.loads(match.group(0))
    assert isinstance(compiled.get("members"), list) and compiled["members"], compiled

    # RUNNABLE: the SAME validator the importer uses confirms the compiled team is assemblable
    members = [OHMMember(**m) for m in compiled["members"]]
    report = assemble_and_report(
        "compiled-from-prose", members, owner_organization_id=org, shape="compiled"
    )
    assert report.report.would_block is False, f"the compiled team is runnable — {report.report}"
    assert report.manifest is not None
    compiled_manifest = report.manifest
    assert load_ohm(compiled_manifest.model_dump(mode="json")).is_team()

    # CTO #3 — "a manifest that itself RUNS": the compiled manifest is POSTed as its OWN team-run
    # and reaches SUCCEEDED. We synthesise a reasoning-only sub-harness per compiled member (its
    # sub-goal as the body, tools=[] — the gate already proved the declared tool ceilings resolve)
    # so the compiled TEAM ORCHESTRATION (members + the depends_on DAG + hand-offs) runs end-to-end.
    from oraclous_ohm.import_.mapping import build_subharness

    compiled_subs = {
        m.role: build_subharness(
            m.role,
            owner_organization_id=org,
            body=(
                m.subgoal or f"You are the {m.role}. Complete your part of the objective, reply."
            ),
            tools=[],
        ).model_dump(mode="json")
        for m in compiled_manifest.members
    }
    cdoc = compiled_manifest.model_dump(mode="json")
    cdoc["models"] = [_model(cred)]
    cgid = c.post("/api/v1/graphs", json={"name": "compiled-team-run"}).json()["id"]
    ccreated = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": cdoc,
            "sub_harnesses": _bind(compiled_subs, cred),
            "gate_decisions": {},
            "graph_id": cgid,
        },
    )
    assert ccreated.status_code == 202, ccreated.text
    crow = _poll(c, ccreated.json()["id"])
    assert crow["state"] == "SUCCEEDED", f"the compiled manifest must itself run — {crow}"


def test_the_capability_absence_gate_blocks_on_the_deployed_registry(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    # DETERMINISTIC (no LLM, keyless): instantiate core/manifest-validate@1 on the LIVE registry and
    # execute it with a draft that names a tool the catalog never surveyed → the coded gate blocks
    # (ADR-032), would_block True — the load-bearing capability-absence proof on the deployed stack.
    user = register(f"gate{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])

    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}
    assert "Manifest Validate" in by_name, f"manifest-validate not seeded; got {sorted(by_name)}"
    inst = c.post(
        "/api/v1/instances",
        json={
            "capability_id": by_name["Manifest Validate"]["id"],
            "name": "manifest-validate",
            "configuration": {},
            "settings": {},
        },
    )
    assert inst.status_code == 201, inst.text
    iid = inst.json()["id"]

    draft = {
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:x/r@1",
                "tools": ["teleport"],
            },
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:x/w@1",
                "depends_on": ["researcher"],
            },
        ]
    }
    ex = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"draft": draft, "catalog": ["web-research"]}},
    )
    assert ex.status_code == 201, ex.text
    out = ex.json()
    assert out["status"] == "SUCCESS", out  # the validation RAN (would_block is in the output)
    data = out["output_data"]
    assert data["would_block"] is True, (
        f"an unsurveyed tool must block on the deployed gate — {out}"
    )
    assert any("F-CAPABILITY-MISSING" in b for b in data["blocking"]), out
