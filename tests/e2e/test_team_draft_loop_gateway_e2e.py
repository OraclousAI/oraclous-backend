"""Team-draft loop END-TO-END through the API GATEWAY on the DEPLOYED docker stack — NO fakes.

#635 (C-1): the four previously-Python-only steps, driven exactly as a browser will drive them —
through `:8006` with a real JWT from a real registration, nothing mocked, nothing DB-direct
(FUCK_CLAUDE_FUCK_PAPERCLIP rule 5).

Deterministic (keyless) legs: the draft store CRUD + verdict, the born-bounded list, the typed
refine (blocked-unsurveyed vs applied), from-run's curated 422s, and org isolation. The full
compile → draft → refine-nl → refine → GO loop exercises real LLM members, so it rides the BYOM
marker with a real OpenRouter key through the gateway (rule 8 — fake LLM is never a proof).

Deterministic legs — bring the stack up fake and run without the byom marker:
    HARNESS_LLM_MODE=fake docker compose -f deploy/docker-compose.yml \
        -f deploy/docker-compose.dev-ports.yml up -d
    uv run pytest tests/e2e -m "e2e and not byom"   (auto-skips when the gateway is down)
BYOM legs — REQUIRE the harness in LIVE mode + OPENROUTER_API_KEY (rule 8: a fake-mode run can
never pass them — the fake client emits prose, never the compiled JSON):
    scripts/e2e.sh --byom   (flips the harness live, runs -m byom, restores fake)
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_MODEL = "openrouter/openai/gpt-4o-mini"

# the #594 proof's objective — the compile → runnable-team loop this issue moves behind endpoints
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


def _team(org: str, members: list[dict]) -> dict:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "draft-loop-team",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": members,
        "runtime": {"entrypoint": members[0]["role"]},
    }


def _agent(role: str, deps: list[str] | None = None, tools: list[str] | None = None) -> dict:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "subgoal": f"do {role}",
        "depends_on": deps or [],
        "tools": tools or [],
    }


def _poll(c: httpx.Client, run_id: str, tries: int = 160) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


# ── deterministic (keyless): the store + typed refine + isolation ─────────────


def test_draft_store_crud_verdict_and_typed_refine_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"drafts{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    org = user["org_id"]

    # CREATE — the write response embeds the shared validator's verdict
    created = c.post(
        "/v1/engine/team-drafts",
        json={
            "name": "digest team",
            "manifest": _team(org, [_agent("researcher"), _agent("writer", ["researcher"])]),
            "sub_harnesses": {},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    draft = body["draft"]
    assert draft["version"] == 1 and draft["name"] == "digest team"
    assert body["would_block"] is False and body["blocking"] == []
    assert isinstance(body["report"], str) and body["report"]

    # GET — the full document + verdict
    fetched = c.get(f"/v1/engine/team-drafts/{draft['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["draft"]["manifest"]["metadata"]["kind"] == "team"

    # LIST — born-bounded {team_drafts, total}, lean rows
    listed = c.get("/v1/engine/team-drafts?limit=1&offset=0")
    assert listed.status_code == 200
    page = listed.json()
    assert page["total"] == 1 and len(page["team_drafts"]) == 1
    row = page["team_drafts"][0]
    assert row["member_count"] == 2 and "manifest" not in row

    # PUT — full replace bumps the version and re-validates
    replaced = c.put(
        f"/v1/engine/team-drafts/{draft['id']}",
        json={
            "name": "digest team v2",
            "manifest": _team(org, [_agent("researcher")]),
            "sub_harnesses": {},
        },
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["draft"]["version"] == 2

    # REFINE (typed op) — an unsurveyed tool is BLOCKED, the draft untouched
    blocked = c.post(
        f"/v1/engine/team-drafts/{draft['id']}/refine",
        json={"edit_op": {"op": "add_member", "role": "rogue", "tools": ["never-surveyed-tool"]}},
    )
    assert blocked.status_code == 200, blocked.text
    verdict = blocked.json()
    assert verdict["applied"] is False and verdict["would_block"] is True
    assert any("F-CAPABILITY-MISSING" in b for b in verdict["blocking"])
    assert verdict["draft"]["version"] == 2  # untouched

    # REFINE (typed op) — a reasoning-only member APPLIES, preserve-the-rest + version bump
    applied = c.post(
        f"/v1/engine/team-drafts/{draft['id']}/refine",
        json={
            "edit_op": {"op": "add_member", "role": "fact-checker", "depends_on": ["researcher"]}
        },
    )
    assert applied.status_code == 200, applied.text
    outcome = applied.json()
    assert outcome["applied"] is True and outcome["would_block"] is False
    assert outcome["draft"]["version"] == 3
    roles = [m["role"] for m in outcome["draft"]["manifest"]["members"]]
    assert roles == ["researcher", "fact-checker"]
    assert "fact-checker" in outcome["draft"]["sub_harnesses"]  # synthesized server-side

    # DELETE — 204, then the read is a 404
    assert c.delete(f"/v1/engine/team-drafts/{draft['id']}").status_code == 204
    assert c.get(f"/v1/engine/team-drafts/{draft['id']}").status_code == 404


def test_from_run_ineligible_runs_are_curated_422s(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"fromrun{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    org = user["org_id"]

    # a gate-only team pauses immediately (keyless) — a PAUSED run cannot seed a draft
    paused = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": _team(
                org,
                [{"role": "author-gate", "kind": "human", "human_role": "author"}],
            ),
            "sub_harnesses": {},
            "gate_decisions": {},
        },
    )
    assert paused.status_code == 202, paused.text
    row = _poll(c, paused.json()["id"])
    assert row["state"] == "PAUSED"

    resp = c.post("/v1/engine/team-drafts/from-run", json={"team_run_id": row["id"]})
    assert resp.status_code == 422, resp.text

    # an unknown run id is a 404, not a 500
    missing = c.post("/v1/engine/team-drafts/from-run", json={"team_run_id": str(uuid.uuid4())})
    assert missing.status_code == 404, missing.text

    # nothing was persisted by either attempt
    assert c.get("/v1/engine/team-drafts").json()["total"] == 0


def test_drafts_are_org_isolated_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    a = register(f"orga{uuid.uuid4().hex[:10]} u")
    b = register(f"orgb{uuid.uuid4().hex[:10]} u")
    ca, cb = gateway_client(a["token"]), gateway_client(b["token"])

    created = ca.post(
        "/v1/engine/team-drafts",
        json={
            "name": "a-only",
            "manifest": _team(a["org_id"], [_agent("researcher")]),
            "sub_harnesses": {},
        },
    )
    assert created.status_code == 201
    draft_id = created.json()["draft"]["id"]

    assert cb.get("/v1/engine/team-drafts").json()["total"] == 0  # org B sees ZERO
    assert cb.get(f"/v1/engine/team-drafts/{draft_id}").status_code == 404  # invisible, not 403
    assert cb.delete(f"/v1/engine/team-drafts/{draft_id}").status_code == 404
    assert ca.get(f"/v1/engine/team-drafts/{draft_id}").status_code == 200  # A keeps its draft


# ── the full loop (real BYOM LLM through the gateway — rule 8) ────────────────


def _refine_nl(c: httpx.Client, draft_id: str, payload: dict, tries: int = 40) -> dict:
    """refine-nl with the 202-collect protocol: a slow op-drafter returns 202 +
    op_drafter_run_id; re-call with the id until the op lands (bounded)."""
    resp = c.post(f"/v1/engine/team-drafts/{draft_id}/refine-nl", json=payload)
    for _ in range(tries):
        if resp.status_code != 202:
            return resp.json() if resp.status_code == 200 else pytest.fail(resp.text)
        run_id = resp.json()["op_drafter_run_id"]
        time.sleep(3)
        resp = c.post(
            f"/v1/engine/team-drafts/{draft_id}/refine-nl",
            json={"op_drafter_run_id": run_id, "dry_run": payload.get("dry_run", False)},
        )
    pytest.fail(f"the op-drafter never settled: {resp.status_code} {resp.text}")


@requires_byom
@pytest.mark.byom
def test_the_whole_loop_compile_draft_refine_go_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The #635 Definition of Done, as ONE user journey: describe (compiler-run 202) → poll →
    draft-from-run (peel+validate+persist) → read the draft → refine-nl dry-run (the typed-op
    preview) → refine apply → GO the draft body via /v1/engine/team-runs → SUCCEEDED."""
    user = register(f"loop{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    gid = c.post("/api/v1/graphs", json={"name": "draft-loop"}).json()["id"]

    # 1) DESCRIBE — the compiler assembles + submits server-side; 202 = a normal team run
    compiled = c.post(
        "/v1/engine/compiler-runs",
        json={"objective": _OBJECTIVE, "models": [_model(cred)], "graph_id": gid},
    )
    assert compiled.status_code == 202, compiled.text
    run = _poll(c, compiled.json()["id"])
    assert run["state"] == "SUCCEEDED", f"the compiler team must run — {run}"

    # 2) DRAFT-FROM-RUN — the reviewer's compiled JSON becomes a stored, validated draft
    seeded = c.post(
        "/v1/engine/team-drafts/from-run",
        json={"team_run_id": run["id"], "name": "compiled digest team"},
    )
    assert seeded.status_code == 201, seeded.text
    envelope = seeded.json()
    assert envelope["would_block"] is False, envelope
    draft = envelope["draft"]
    assert draft["manifest"]["members"], "the compiled team has members"
    assert set(draft["sub_harnesses"]) >= {
        m["role"] for m in draft["manifest"]["members"] if m["kind"] == "agent"
    }

    # 3) the draft reads back whole
    assert c.get(f"/v1/engine/team-drafts/{draft['id']}").status_code == 200

    # 4) REFINE-NL dry-run — the op-drafter (real LLM) turns prose into ONE typed op, previewed
    preview = _refine_nl(
        c,
        draft["id"],
        {
            "instruction": (
                "Add a reasoning-only member named 'fact-checker' (no tools) that runs after"
                " every other member and double-checks the digest for unsupported claims."
            ),
            "models": [_model(cred)],
            "dry_run": True,
        },
    )
    assert preview["applied"] is False  # a preview never mutates
    op = preview["op"]
    assert op.get("op") in {"add_member", "set_fan_out", "change_kind", "add_depends_on"}, op

    # 5) REFINE — the previewed op applies deterministically; version bumps
    version_before = c.get(f"/v1/engine/team-drafts/{draft['id']}").json()["draft"]["version"]
    applied = c.post(f"/v1/engine/team-drafts/{draft['id']}/refine", json={"edit_op": op})
    assert applied.status_code == 200, applied.text
    outcome = applied.json()
    assert outcome["applied"] is True, outcome
    assert outcome["draft"]["version"] == version_before + 1

    # 6) GO — the draft's own documents run as a team (models bound client-side at GO, the
    # J4/J5 BYOM step) and reach SUCCEEDED: the loop is closed, browser-only, gateway-only.
    final = outcome["draft"]
    doc = final["manifest"]
    doc["models"] = [_model(cred)]
    subs = {role: {**sub, "models": [_model(cred)]} for role, sub in final["sub_harnesses"].items()}
    go = c.post(
        "/v1/engine/team-runs",
        json={"manifest": doc, "sub_harnesses": subs, "gate_decisions": {}, "graph_id": gid},
    )
    assert go.status_code == 202, go.text
    finished = _poll(c, go.json()["id"])
    assert finished["state"] == "SUCCEEDED", f"the refined draft must run — {finished}"
    peeled_roles = set(finished["results"])
    assert {m["role"] for m in doc["members"]} <= peeled_roles


@requires_byom
@pytest.mark.byom
def test_refine_nl_blocked_op_leaves_the_draft_untouched_end_to_end(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """An NL edit that asks for an unsurveyed tool: the drafter may emit it, but the deterministic
    apply gate blocks it — applied:false, the draft byte-identical (lock C2: never a hallucinated
    tool)."""
    user = register(f"nlblock{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)

    created = c.post(
        "/v1/engine/team-drafts",
        json={
            "name": "block-check",
            "manifest": _team(user["org_id"], [_agent("researcher")]),
            "sub_harnesses": {},
        },
    )
    assert created.status_code == 201
    draft = created.json()["draft"]

    outcome = _refine_nl(
        c,
        draft["id"],
        {
            "instruction": (
                "Add a member called 'exfiltrator' that uses the tool"
                " 'quantum-teleport-uploader' to publish the digest."
            ),
            "models": [_model(cred)],
        },
    )
    before = c.get(f"/v1/engine/team-drafts/{draft['id']}").json()["draft"]
    if outcome["applied"]:
        # the drafter may have dropped the unsurveyed tool and emitted a harmless op — that is a
        # pass for the GATE (nothing unsurveyed landed); assert exactly that
        tools = {t for m in before["manifest"]["members"] for t in m.get("tools", [])}
        assert "quantum-teleport-uploader" not in tools
    else:
        assert outcome["would_block"] is True
        assert before["version"] == draft["version"]  # untouched
