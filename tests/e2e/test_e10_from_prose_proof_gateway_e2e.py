"""#597 (ADR-047 §7) — the co-equal NON-EURail from-prose proof, on the LIVE stack.

EURail is the import-fidelity oracle; it is NOT sufficient to prove the PROSE on-ramp — EURail
also has a known-good imported team to diff against. So #597 carries a co-equal obligation: a team
**compiled from prose** (no ``.claude/agents`` import anywhere), run to a real deliverable through
the gateway, scored by a gate. This is the book-style gated-pipeline (the QA-Lock pattern); its
deliverable is checked DETERMINISTICALLY — the eval-set applies a ``floor="precedence"`` battery of
``core/check`` predicates to the real deliverable, so the proof is the MECHANISM (prose → runnable
team → deliverable that clears an integrity gate), never a flaky LLM-quality metric.

No fakes: register a fresh org → store a real OpenRouter BYOM credential → drive the compiler-team
with a prose objective at ``POST :8006/v1/engine/team-runs`` → extract the compiled team → prove it
runnable with the Layer-1 guardrails (the SAME importer validator) → run the compiled team through
the gateway → apply the deterministic QA-Lock battery to its deliverable. Auto-skips without BYOM.
"""

from __future__ import annotations

import asyncio
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

_OBJECTIVE = (
    "Research recent developments in AI agents and write a structured plain-text briefing: "
    "a one-paragraph summary followed by three labelled key points."
)


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
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED", "PARTIAL"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


def _deliverable(row: dict) -> str:
    """The compiled team's collective output — every member's text joined (the QA-Lock gate scores
    the team's deliverable, not one member)."""
    parts = []
    for val in (row.get("results") or {}).values():
        out = val.get("output") if isinstance(val, dict) else val
        if out:
            parts.append(str(out))
    return "\n\n".join(parts)


def _qa_lock_battery():
    # the eval-set's QA-Lock: a deterministic floor='precedence' battery. CRITICAL integrity gate
    # (no unresolved markers) BLOCKS; substance/topic gates are reported, non-blocking. No judge.
    from oraclous_ohm.manifest import OHMGateBattery, OHMGateCheck

    return OHMGateBattery(
        name="from-prose-qa-lock",
        floor="precedence",
        checks=[
            OHMGateCheck(
                name="no-unresolved-markers",
                kind="deterministic",
                check_ref="core/check/no-forbidden",
                params={"terms": ["TODO", "PLACEHOLDER", "DISPUTED", "needs-source", "TBD"]},
                severity="CRITICAL",
            ),
            OHMGateCheck(
                name="substantive",
                kind="deterministic",
                check_ref="core/check/min-length",
                params={"min": 120},
                severity="MAJOR",
            ),
            OHMGateCheck(
                name="on-topic",
                kind="deterministic",
                check_ref="core/check/contains-all",
                params={"terms": ["agent"]},
                severity="MINOR",
            ),
        ],
    )


@requires_byom
def test_a_prose_objective_compiles_and_runs_to_a_deliverable_that_clears_a_qa_lock(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    from oraclous_eval import run_plan_guardrails
    from oraclous_ohm.compiler import build_compiler_team
    from oraclous_ohm.gate_battery import run_battery
    from oraclous_ohm.import_ import assemble_and_report
    from oraclous_ohm.import_.mapping import build_subharness
    from oraclous_ohm.manifest import OHMMember
    from oraclous_ohm.seeds import seed_capability_inventory, survey_catalog

    user = register(f"fromprose{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])

    live = [x["name"] for x in c.get("/api/v1/capabilities").json()["capabilities"]]
    catalog = survey_catalog(seed_capability_inventory(), live)

    # STEP 1: compile the PROSE objective (no import anywhere) → the compiler-team runs on :8006.
    manifest, subs = build_compiler_team(org, objective=_OBJECTIVE, catalog=catalog)
    doc = manifest.model_dump(mode="json")
    doc["models"] = [_model(cred)]
    gid = c.post("/api/v1/graphs", json={"name": "from-prose-compile"}).json()["id"]
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
    assert row["state"] in {"SUCCEEDED", "PARTIAL"}, f"the compiler team must run — {row}"

    # STEP 2: extract the compiled team (reviewer's validated team, or the drafter's on degrade).
    results = row.get("results") or {}
    compiled = None
    for member in ("reviewer", "manifest-drafter"):
        raw = results.get(member)
        text = (raw.get("output") if isinstance(raw, dict) else raw) if raw else None
        if not text:
            continue
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                cand = json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
            if isinstance(cand.get("members"), list) and cand["members"]:
                compiled = cand
                break
    assert compiled, f"prose compiled to a team — {results}"

    # STEP 3: Layer-1 guardrails on the LIVE compiled team — the SAME importer validator proves it
    # runnable (acyclic DAG, every tool surveyed, caps within the pool). The from-prose on-ramp is
    # held to the identical bar as the import on-ramp.
    guard = run_plan_guardrails(compiled, owner_organization_id=org, catalog=catalog)
    assert not guard.would_block, f"the compiled team must pass Layer-1 — {guard.render()}"

    members = [OHMMember(**m) for m in compiled["members"]]
    report = assemble_and_report("from-prose", members, owner_organization_id=org, shape="compiled")
    assert report.report.would_block is False and report.manifest is not None
    compiled_manifest = report.manifest

    # STEP 4: RUN the compiled team (reasoning-only sub-harnesses; the gate proved tools resolve).
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
    cgid = c.post("/api/v1/graphs", json={"name": "from-prose-run"}).json()["id"]
    crun = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": cdoc,
            "sub_harnesses": _bind(compiled_subs, cred),
            "gate_decisions": {},
            "graph_id": cgid,
        },
    )
    assert crun.status_code == 202, crun.text
    crow = _poll(c, crun.json()["id"])
    assert crow["state"] in {"SUCCEEDED", "PARTIAL"}, f"the compiled team must itself run — {crow}"

    deliverable = _deliverable(crow)
    assert len(deliverable) >= 50, f"the compiled team produced a real deliverable — {crow}"

    # STEP 5: the eval-set applies the deterministic QA-Lock battery to the REAL deliverable. The
    # run cleared the integrity gate (CRITICAL no-forbidden) — the mechanism, no judge, no flake.
    verdict = asyncio.run(run_battery(_qa_lock_battery(), deliverable, evaluate=_no_judge))
    assert verdict.passed is True, f"the deliverable clears the QA-Lock — {verdict.failures}"
    assert verdict.blocking_severity is None
    critical = [v for v in verdict.check_verdicts if v.severity == "CRITICAL"]
    assert critical and all(v.passed for v in critical), "the CRITICAL integrity gate passed"
    assert len(verdict.check_verdicts) == 3, (
        "every QA-Lock gate evaluated the deliverable (no stub)"
    )


async def _no_judge(check: object, output: str) -> float:
    raise AssertionError("the QA-Lock battery is deterministic — no judge call is expected")
