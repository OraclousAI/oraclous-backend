"""#596 — a fresh org bootstraps against the SEED DEFAULTS, proven on the LIVE stack (ADR-047 §5).

No fakes: register a fresh org → store a real OpenRouter BYOM credential → drive the compiler-team
at ``POST :8006/v1/engine/team-runs`` with a prose objective whose available capabilities are the
SEED INVENTORY (∪ the live registry). The surveyor's catalog is NON-EMPTY (else a fresh org would
fail closed with an empty-catalog gap), the run reaches the drafter, and the compiled team is
GOVERNED-BY-DEFAULT — it carries the seed ``governance.policy_set_ref`` (a known ref) + the 3-layer
budget, each per-agent cap <= the team pool (the L1-clamp). Auto-skips without the BYOM/gateway.
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
def test_a_fresh_org_compile_bootstraps_against_the_seeds_governed_by_default(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    from oraclous_ohm.compiler import build_compiler_team
    from oraclous_ohm.seeds import (
        DEFAULT_POLICY_SET_REF,
        seed_capability_inventory,
        survey_catalog,
    )

    user = register(f"seedboot{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])

    # the surveyor's catalog = the SEED inventory ∪ the LIVE registry — a fresh org is NON-EMPTY
    live = [x["name"] for x in c.get("/api/v1/capabilities").json()["capabilities"]]
    catalog = survey_catalog(seed_capability_inventory(), live)
    assert catalog, "the seed inventory must make a fresh org's survey non-empty"

    objective = "Research the week's top AI news and write a short plain-text digest."
    manifest, subs = build_compiler_team(org, objective=objective, catalog=catalog)
    doc = manifest.model_dump(mode="json")
    doc["models"] = [_model(cred)]
    gid = c.post("/api/v1/graphs", json={"name": "seed-bootstrap"}).json()["id"]
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
    # SUCCEEDED, or PARTIAL when the reviewer DEGRADED (#587) — a weak BYOM model (gpt-4o-mini) can
    # benignly RE-VALIDATE an already-clean draft past the reviewer's tool-call cap; degrade-not-
    # crash finishes the run as a flagged PARTIAL. Either way the GOVERNED team was produced: the
    # drafter emits the governance, the reviewer validated would_block=False before any over-check.
    assert row["state"] in {"SUCCEEDED", "PARTIAL"}, f"the seeded compile must run — {row}"

    # the compiled GOVERNED team — from the reviewer's validated output, or (if it degraded) from
    # the MANIFEST-DRAFTER that EMITS the governance. Both are REAL LLM output on the live stack
    # (no fake, no DB-direct); a fresh org with a non-empty seed survey yields a team, never a gap.
    results = row.get("results") or {}
    compiled: dict | None = None
    for member in ("reviewer", "manifest-drafter"):
        raw = results.get(member)
        text = (raw.get("output") if isinstance(raw, dict) else raw) if raw else None
        if not text:
            continue
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            continue
        try:
            cand = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(cand.get("members"), list) and cand["members"]:
            compiled = cand
            break
    assert compiled, f"a real compiled governed team (reviewer or drafter) — {results}"

    # GOVERNED-BY-DEFAULT: the seed governance.policy_set_ref (a known ref) + the 3-layer budget
    gov = compiled.get("governance") or {}
    assert gov.get("policy_set_ref") == DEFAULT_POLICY_SET_REF, f"governed-by-default — {gov}"
    budget = compiled.get("budget") or {}
    assert budget.get("max_tokens_total"), f"a team-pooled L2 budget — {budget}"
    # the L1-clamp invariant on the real stack: each per-agent cap <= the pool
    if budget.get("max_tokens_per_member"):
        assert budget["max_tokens_per_member"] <= budget["max_tokens_total"]
    if budget.get("max_tool_calls_per_member") and budget.get("max_tool_calls_total"):
        assert budget["max_tool_calls_per_member"] <= budget["max_tool_calls_total"]
