"""#597 (ADR-047 §7) — Layer-3: the ship-bar runner on the LIVE stack (sample-N + a real judge).

The unit tests prove the runner's logic with fakes; this proves the SAME runner end-to-end on the
deployed stack: a prose objective is compiled N times through ``:8006`` (each a real compiler-team
run), each compiled plan is gated by the Layer-1 guardrails and then scored for plan-adequacy by a
REAL OpenRouter judge panel (the same ``OpenAIEvalJudge`` KRS's ``/internal/v1/evaluate`` wraps),
and the K-of-N ship-bar verdict is computed. We assert the MECHANISM — N samples, each scored by the
real judge, a recorded ShipBarVerdict — not a flaky pass/fail of one non-deterministic generation.

Plan-adequacy only (no per-sample run): the from-prose proof already exercises compile→run→gate;
this isolates Layer-3's machinery (sample-N, de-biased judging, K-of-N) on real compiles.
Auto-skips without the BYOM key / a reachable gateway.
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


def _compile_team(c: httpx.Client, cred: str, org: uuid.UUID, catalog: list, prose: str) -> dict:
    """Drive the compiler-team on :8006 for a prose objective and return the compiled team dict
    (the reviewer's validated team, or the drafter's on a reviewer degrade)."""
    from oraclous_ohm.compiler import build_compiler_team

    manifest, subs = build_compiler_team(org, objective=prose, catalog=catalog)
    doc = manifest.model_dump(mode="json")
    doc["models"] = [_model(cred)]
    gid = c.post("/api/v1/graphs", json={"name": "evalset-compile"}).json()["id"]
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
    results = row.get("results") or {}
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
                return cand
    raise AssertionError(f"prose compiled to a team — {results}")


@requires_byom
def test_the_ship_bar_runner_samples_and_judges_real_compiles_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    from oraclous_eval import (
        CompiledPlan,
        EvalSetManifest,
        EvalSetRunner,
        JudgeConfig,
        Objective,
        ShipBar,
        make_judge,
    )
    from oraclous_eval.types import Dimension, Rubric
    from oraclous_ohm.seeds import seed_capability_inventory, survey_catalog

    user = register(f"evalset{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])
    live = [x["name"] for x in c.get("/api/v1/capabilities").json()["capabilities"]]
    catalog = survey_catalog(seed_capability_inventory(), live)

    plan_rubric = Rubric(
        pass_threshold=0.6,
        dimensions=[
            Dimension(
                name="role-coverage",
                severity="critical",
                prompt="The input is a compiled team manifest (JSON) for a research-and-write "
                "objective. Score 0–1: do the members cover the roles the objective needs?",
            ),
            Dimension(
                name="capability-fit",
                severity="major",
                prompt="Score 0–1: does each member declare sensible tools for its sub-goal (no "
                "hallucinated tool)?",
            ),
        ],
    )
    objective = Objective(
        id="research-digest",
        prose="Research recent AI-agent developments and write a short plain-text briefing.",
        plan_rubric=plan_rubric,
        run_rubric=None,  # plan-adequacy only — isolate Layer-3's sample-N/judge machinery
    )
    eval_set = EvalSetManifest(
        name="e10-deployed-slice",
        objectives=[objective],
        ship_bar=ShipBar(n_samples=3, k_pass=2, min_score=0.6, max_variance=0.5),
    )

    # the injected deployed compile — the runner stays generator-agnostic; here it drives :8006.
    async def _compile(prose: str) -> CompiledPlan:
        return CompiledPlan(manifest=_compile_team(c, cred, org, catalog, prose), catalog=catalog)

    # a REAL judge panel against OpenRouter (the same OpenAIEvalJudge KRS wraps); 2 judges → the
    # de-bias dimension-rotation + the variance signal are exercised on real LLM scores.
    judges = [make_judge(JudgeConfig(api_key=_OR_KEY)) for _ in range(2)]
    assert all(j is not None for j in judges), "BYOM judge must resolve"

    runner = EvalSetRunner(eval_set, judges, compile=_compile, run=None, owner_organization_id=org)
    result = asyncio.run(runner.run())

    # the MECHANISM: one objective, N=3 samples, each compiled on the live stack, passed the Layer-1
    # guardrails (not guardrail-blocked), and scored for plan-adequacy by the REAL judge panel.
    assert result.summary["judges"] == 2 and result.summary["samples_per_objective"] == 3
    obj = result.objectives[0]
    assert len(obj.samples) == 3
    assert all(not s.blocked_by_guardrails for s in obj.samples), "live compiles pass Layer-1"
    assert all(s.plan is not None for s in obj.samples), "the real judge scored every sample"
    # the real panel produced two VALID scores per sample (de-biased), and the ship-bar verdict is
    # internally consistent — the runner machinery ran end-to-end on real LLM scores.
    for s in obj.samples:
        assert s.plan is not None and len(s.plan.judge_scores) == 2
        assert all(0.0 <= sc <= 1.0 for sc in s.plan.judge_scores), "real, valid judge scores"
    assert obj.recommendation in {"ship", "revise", "escalate", "inconclusive"}
    assert 0.0 <= obj.median_score <= 1.0
    assert obj.consensus_ratio == round(obj.pass_count / 3, 4)
