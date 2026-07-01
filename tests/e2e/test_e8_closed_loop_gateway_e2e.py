"""#604 (E8) — closed-loop verdict-consumption: a settled run BRANCHES on its verdict, on the LIVE

ADR-048 decision 5: the E4 verdict, stored-but-ignored until now, is CONSUMED at settle. A
below-threshold verdict re-dispatches (re_task) the faulted members with a revised objective
(never a blind re-run) drawing the #585 pool, bounded so the loop MUST terminate (livelock,
MAX ceiling + pool exhaustion → escalate); a HITL-class verdict (a CRITICAL floor failure) escalates
the run to PAUSED for a human, never a retry.

No fakes, gateway ``:8006`` with real OpenRouter BYOM. The gate is a DETERMINISTIC
``OHMGateBattery`` (a coded ``core/check`` predicate, NOT an LLM judge) the output cannot clear:
is deterministically below-threshold and the branch is deterministic (no judge non-determinism):

* a MAJOR failing check → recommended_action ``block`` → re_task re-dispatches; the re-run fails the
  same check → the same below-threshold fingerprint recurs → LIVELOCK → escalate → PAUSED with the
  verdict-escalation sentinel. Asserts ``re_dispatch_count >= 1`` (re-dispatched, not blindly left
  SUCCEEDED) + the pool grew (real tokens each drive) + it HALTED (never spins).
* a CRITICAL failing check → recommended_action ``escalate_human`` → PAUSED immediately
  (``re_dispatch_count == 0``), no re-dispatch. Auto-skips without the BYOM key / a gateway.
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

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_SENTINEL = "__verdict_escalation__"
# a token the member's output will never contain → the deterministic core/check fails every drive.
_IMPOSSIBLE = "ZZQX9_IMPOSSIBLE_TOKEN_7K7K"


def _byom_model(credential_id: str) -> dict:
    return {
        "role": "primary",
        "binding": "openrouter/openai/gpt-4o-mini",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
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


def _worker_studio(root: Path) -> None:
    """A one-member team whose output is a short line — it will never contain the impossible token,
    so the deterministic gate below decisively fails it every drive."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "worker.md").write_text(
        "---\nname: worker\nmodel: sonnet\n---\nReply with one short sentence about the weather.\n"
    )
    (root / "teams" / "1-work").mkdir(parents=True)
    (root / "teams" / "1-work" / "charter.md").write_text(
        "# Team I — Work\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `worker` | subagent | sonnet | work |\n"
    )


def _body(root: Path, cred: str, org: uuid.UUID, severity: str, n_checks: int = 1) -> dict:
    _worker_studio(root)
    imported = import_setup(root, owner_organization_id=org, name="cl", substrate="file")
    assert imported.manifest is not None
    manifest = imported.manifest.model_dump(mode="json")
    # a DETERMINISTIC battery gate the output cannot clear (a coded predicate, no judge). floor and
    # → any failing check blocks; MAJOR → block → re_task, CRITICAL → escalate_human.
    # ``n_checks`` > 1 makes a LARGE battery: every distinctly-named check fails, so the failing-
    # shape fingerprint spans N dims. At 15 checks the RAW shape JSON would exceed VARCHAR(256) and
    # overflow the ``last_verdict_fingerprint`` CAS write → drop the re-dispatch. The bounded
    # 64-char digest (CTO review #622) must let this re_task on the LIVE stack. (CRITICAL uses 1.)
    checks = [
        {
            "name": f"must-contain-{i:02d}-a-reasonably-descriptive-check-name",
            "kind": "deterministic",
            "check_ref": "core/check/contains-all",
            "params": {"terms": [_IMPOSSIBLE]},
            "severity": severity,
        }
        for i in range(n_checks)
    ]
    manifest["orchestration"] = {
        **(manifest.get("orchestration") or {}),
        "success_criteria": "battery:gate",
    }
    manifest["batteries"] = {"gate": {"name": "gate", "floor": "and", "checks": checks}}
    sub_harnesses = {
        role: {**dict(sub), "models": [_byom_model(cred)]}
        for role, sub in imported.sub_harnesses.items()
    }
    return {"manifest": manifest, "sub_harnesses": sub_harnesses, "gate_decisions": {}}


def _poll(c: httpx.Client, run_id: str, until: set[str], tries: int = 180) -> dict:
    # a re_task loop runs MULTIPLE real-BYOM drives (settle → re_task re-dispatch → re-drive →
    # livelock → escalate) plus queue wait, so it needs a generous window (~6 min) — the terminal
    # PAUSED is deterministic, only its arrival time varies with BYOM latency + worker load.
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in until:
            return row
        time.sleep(2)
    raise AssertionError(f"run {run_id} never reached {until} (last: {row.get('state')})")


@requires_byom
def test_below_threshold_re_task_re_dispatches_then_escalates_on_the_bound(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"loop{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])

    # a LARGE 15-check battery: the failing-shape fingerprint would exceed VARCHAR(256) as raw JSON
    # and overflow the re_task CAS write (silently dropping the re-dispatch) — the bounded 64-char
    # digest (CTO review #622) must let it re_task on the LIVE stack, not overflow + drop.
    created = c.post("/v1/engine/team-runs", json=_body(tmp_path, cred, org, "MAJOR", n_checks=15))
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    # a MAJOR failing gate → block → re_task re-dispatches; the re-run fails identically → LIVELOCK/
    # MAX → escalate → PAUSED with the sentinel. The loop MUST terminate (never spins forever).
    row = _poll(c, run_id, {"PAUSED", "SUCCEEDED", "REJECTED"})
    assert row["state"] == "PAUSED", f"the bounded loop must escalate to PAUSED — {row}"
    assert row["paused_at"] == [_SENTINEL], f"the verdict-escalation sentinel — {row['paused_at']}"
    # it was RE-DISPATCHED (consumed the verdict), not blindly left SUCCEEDED — the mechanism proof
    assert int(row.get("re_dispatch_count") or 0) >= 1, f"must have re-dispatched — {row}"
    # each drive drew real BYOM tokens (the accumulating #585 pool that bounds the loop)
    cost = c.get(f"/v1/engine/team-runs/{run_id}/status").json()["cost"]["tokens"]
    assert int(cost or 0) > 0, f"the re-dispatch loop drew real tokens — {cost}"
    # graded below threshold — a battery verdict keys it ``passed`` (a prose verdict ``pass``)
    v = row["verdict"]
    assert v is not None and v.get("passed", v.get("pass")) is False


@requires_byom
def test_critical_verdict_escalates_to_hitl_immediately_no_redispatch(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"crit{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])

    created = c.post("/v1/engine/team-runs", json=_body(tmp_path, cred, org, "CRITICAL"))
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    # a CRITICAL floor failure → escalate_human → PAUSED for HITL immediately, NEVER a re-dispatch.
    row = _poll(c, run_id, {"PAUSED", "SUCCEEDED", "REJECTED"})
    assert row["state"] == "PAUSED", f"a CRITICAL verdict escalates to PAUSED — {row}"
    assert row["paused_at"] == [_SENTINEL]
    assert int(row.get("re_dispatch_count") or 0) == 0, "a CRITICAL verdict never re-dispatches"
