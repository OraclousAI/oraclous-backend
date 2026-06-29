"""#585 (ADR-031 §D3) — a team-pooled budget HALTS a runaway run mid-flight, through the gateway.

A multi-member sequential team (a→b→c→d→e, real BYOM) with a deliberately low ``max_tokens_total``:
the engine's running pooled tally crosses the ceiling after the first member(s), so the next member
is never dispatched — the run terminates ``COST_BUDGET`` / flagged ``partial`` with FEWER members
run than declared (the un-run ones recorded ``budget_skipped``), not a full overrun and not a silent
success. This is #585's safety property end-to-end on the deployed engine.

(The DoD's fan-out e2e is blocked by a pre-existing gap — a fan-out's ``over`` is not seedable
through the gateway today, see the #585 thread — so the pooled halt is proven via a multi-member
team, which routes every member through the SAME pre-dispatch pooled gate. The fan-out admission
loop is unit-proven in ``test_pooled_budget.py``.)

Real OpenRouter (BYOM through the gateway), nothing mocked (rules 3/5/8). Auto-skips when the
key/gateway is absent (a skip is NOT a pass).
"""

from __future__ import annotations

import os
import pathlib
import re
import tempfile
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.byom]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(
    not _OR_KEY, reason="OPENROUTER_API_KEY unset (the real-model pooled-budget proof)"
)
_MODEL = "openrouter/openai/gpt-4o-mini"
_CHAIN = ["alpha", "bravo", "charlie", "delta", "echo"]
_POOL_TOKENS = 300  # low enough that the chain's running spend crosses it before every member runs


def _cred(c: httpx.Client, user_id: str, key: str, name: str) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": name,
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": key},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _registry_capable(c: httpx.Client, sub: dict) -> list[dict]:
    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.strip().lower()).strip("-")

    reg = {_slug(x["name"]) for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    return [
        cap
        for cap in sub.get("capabilities", [])
        if (cap.get("ref", "").split("/")[-1].split("@")[0]) in reg
    ]


def _write_chain_team() -> pathlib.Path:
    """A real sequential agent chain alpha→bravo→…→echo — each member a small real BYOM call, so the
    running pooled spend accrues member by member and the ceiling halts the chain mid-way."""
    root = pathlib.Path(tempfile.mkdtemp())
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    for i, name in enumerate(_CHAIN):
        nxt = _CHAIN[i + 1] if i + 1 < len(_CHAIN) else None
        handoff = f"\n\n## Handoff\n**Next agent**: {nxt}\n" if nxt else ""
        (adir / f"{name}.md").write_text(
            f"---\nname: {name}\n---\n"
            "Write one short sentence continuing a story about a lighthouse, then stop." + handoff
        )
    return root


def _import_bind(c: httpx.Client, user: dict, or_cred: str) -> tuple[dict, dict]:
    from oraclous_ohm.import_.setup import import_setup

    imported = import_setup(
        _write_chain_team(),
        owner_organization_id=uuid.UUID(user["org_id"]),
        name="pooled-budget-chain",
    )
    model = {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": or_cred},
    }
    subs = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    for sub in subs.values():
        sub["models"] = [model]
        sub["capabilities"] = _registry_capable(c, sub)
    doc = imported.manifest.model_dump(mode="json")
    doc["models"] = [model]
    doc["budget"] = {"max_tokens_total": _POOL_TOKENS}  # the team-pooled ceiling (#585)
    return doc, subs


def _poll(c: httpx.Client, run_id: str, tries: int = 140) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED", "COST_BUDGET"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


def _run(c: httpx.Client, doc: dict, subs: dict, name: str) -> dict:
    gid = c.post("/api/v1/graphs", json={"name": name}).json()["id"]
    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": doc, "sub_harnesses": subs, "gate_decisions": {}, "graph_id": gid},
    )
    assert created.status_code == 202, created.text
    return _poll(c, created.json()["id"])


@requires_byom
def test_pooled_budget_halts_a_runaway_chain(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"poolbud{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "pooled-budget openrouter key")
    doc, subs = _import_bind(c, user, or_cred)

    done = _run(c, doc, subs, "pooled-budget-chain")

    # the engine ran the pre-ceiling members and HALTED at the team-pooled total — not past it.
    assert done["state"] == "COST_BUDGET", f"the chain must halt at the pooled ceiling — {done}"
    assert done["partial"] is True, f"a budget halt is a flagged partial — {done}"
    member_status = done.get("member_status") or {}
    ran = [r for r, s in member_status.items() if s == "succeeded"]
    skipped = [r for r, s in member_status.items() if s == "budget_skipped"]
    assert len(skipped) >= 1, f"at least one member must be un-run by budget — {member_status}"
    assert len(ran) < len(_CHAIN), f"fewer members ran than declared — {member_status}"
