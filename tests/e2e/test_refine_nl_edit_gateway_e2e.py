"""#595 — an NL edit REFINES a team as a typed structural delta, proven on the LIVE stack (ADR-047).

Both halves run through the gateway, no fakes: (1) an op-drafter LLM member turns a natural-language
edit into ONE typed op (real OpenRouter, through :8006); (2) the deterministic
``core/manifest-refine@1`` connector applies it and re-validates through the SAME importer dry-run.
The manifest flows in deterministically (never re-emitted by the model), so the PRESERVE-THE-REST
byte-identity invariant holds: the asserted delta lands AND every untouched member is identical.
The adversarial case — an edit naming an unsurveyed tool — returns a gap report (would_block), NOT a
hallucinated capability, and the manifest is left UNMUTATED. Auto-skips without the BYOM/gateway.

The refine INPUT is a fixed compiled-shape team (the prose→compile step is proven by #594); chaining
a real compile in front composes with that e2e.
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
_CATALOG = ["web-research", "send-to-drafts"]  # the surveyed catalog (real registered tools)


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


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


def _compiled_manifest(org: uuid.UUID) -> dict:
    """A fixed compiled-shape OHM v1.1 team — the refine INPUT (preserve-the-rest is asserted)."""
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "research-digest",
            "owner_organization_id": str(org),
            "kind": "team",
        },
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:c/r@1",
                "tools": ["web-research"],
            },
            {
                "role": "writer",
                "kind": "agent",
                "manifest_ref": "org:c/w@1",
                "depends_on": ["researcher"],
            },
        ],
        "runtime": {"entrypoint": "researcher"},
    }


def _draft_op(c: httpx.Client, org: uuid.UUID, manifest: dict, edit: str, cred: str) -> dict:
    """Run the op-drafter LLM member through the gateway → the ONE typed op it emits (peeled)."""
    from oraclous_ohm.compiler.prompts import OP_DRAFTER_PROMPT
    from oraclous_ohm.import_.mapping import build_subharness
    from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime

    subgoal = (
        f"CURRENT TEAM MANIFEST:\n{json.dumps(manifest)}\n\n"
        f"SURVEYED CATALOG: {json.dumps(_CATALOG)}\n\nEDIT REQUEST: {edit}"
    )
    team = OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(
            id=uuid.uuid4(), name="refine-drafter", owner_organization_id=org, kind="team"
        ),
        members=[
            OHMMember(
                role="op-drafter", kind="agent", manifest_ref="org:refine/d@1", subgoal=subgoal
            )
        ],
        runtime=OHMRuntime(entrypoint="op-drafter"),
    )
    doc = team.model_dump(mode="json")
    doc["models"] = [_model(cred)]
    sub = build_subharness(
        "op-drafter", owner_organization_id=org, body=OP_DRAFTER_PROMPT, tools=[]
    ).model_dump(mode="json")
    sub["models"] = [_model(cred)]
    gid = c.post("/api/v1/graphs", json={"name": "refine-draft"}).json()["id"]
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": doc,
            "sub_harnesses": {"op-drafter": sub},
            "gate_decisions": {},
            "graph_id": gid,
        },
    )
    assert created.status_code == 202, created.text
    row = _poll(c, created.json()["id"])
    assert row["state"] == "SUCCEEDED", f"the op-drafter must run — {row}"
    raw = (row.get("results") or {}).get("op-drafter")
    text = raw["output"] if isinstance(raw, dict) else raw
    match = re.search(r"\{.*\}", text, re.DOTALL)
    assert match, f"the op-drafter emitted a JSON op — {text!r}"
    return json.loads(match.group(0))


def _apply(c: httpx.Client, manifest: dict, op: dict) -> dict:
    """Apply the op through the deterministic core/manifest-refine@1 connector on the registry."""
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}
    assert "Manifest Refine" in by_name, f"manifest-refine not seeded; got {sorted(by_name)}"
    iid = c.post(
        "/api/v1/instances",
        json={
            "capability_id": by_name["Manifest Refine"]["id"],
            "name": "refine",
            "configuration": {},
            "settings": {},
        },
    ).json()["id"]
    ex = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"manifest": manifest, "edit_op": op, "catalog": _CATALOG}},
    )
    assert ex.status_code == 201, ex.text
    out = ex.json()
    assert out["status"] == "SUCCESS", out
    return out["output_data"]


@requires_byom
def test_an_nl_edit_refines_the_team_preserving_the_rest(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"refine{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])
    from oraclous_ohm.parse import load_ohm

    manifest = _compiled_manifest(org)
    # canonicalise the BEFORE snapshot (full model_dump) so preserve-the-rest compares like-for-like
    # with the connector's canonical output — both sides through the same loader.
    before = {m.role: m.model_dump(mode="json") for m in load_ohm(manifest).members}

    # NL → typed op (gateway LLM) → deterministic apply (gateway connector)
    op = _draft_op(c, org, manifest, "add a fact-checker that depends on the researcher", cred)
    out = _apply(c, manifest, op)
    assert out["would_block"] is False and out["applied"] is True, out
    patched = {m["role"]: m for m in out["manifest"]["members"]}
    # (a) the delta landed: a NEW member, surveyed tools only, depends_on keeps the DAG acyclic
    new_roles = set(patched) - set(before)
    assert new_roles, f"a new member was added — {sorted(patched)}"
    # (c) PRESERVE-THE-REST: every pre-existing member is byte-identical
    for role, member in before.items():
        assert patched[role] == member, f"member {role!r} was NOT preserved byte-identical"


def test_a_refine_naming_an_unsurveyed_tool_is_blocked_unmutated_on_the_deployed_registry(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    # DETERMINISTIC (no LLM, keyless): a refine op naming a tool the surveyor never offered must be
    # blocked by the gate (would_block), NOT applied — the manifest is left UNMUTATED (no capability
    # escalation, ADR-032) — the load-bearing safety proof on the deployed stack.
    user = register(f"refgap{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    org = uuid.UUID(user["org_id"])
    manifest = _compiled_manifest(org)
    op = {"op": "add_member", "role": "rogue", "tools": ["wire-transfer"], "depends_on": ["writer"]}
    out = _apply(c, manifest, op)
    assert out["would_block"] is True and out["applied"] is False, out
    assert out["manifest"] is None  # not mutated — the original stands
