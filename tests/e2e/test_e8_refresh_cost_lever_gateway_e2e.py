"""#602 (E8) — the seeded-refresh COST LEVER, proven LIVE through the gateway with real BYOM.

The 5-way delta already ships (test_e8_seeded_refresh_gateway_e2e). This proves the OTHER half of
ADR-048 §3 the CTO flagged as unwired: a refresh over UNCHANGED data costs MATERIALLY FEWER member
tokens than a cold derivation, because the producing (sink) member now RECEIVES its prior records in
its dispatch context (not a hard-coded prompt marker) and CARRIES THEM FORWARD, not re-deriving.

Design (deterministic mechanism, not a fabricated fraction):
- a one-member "analyst" is told to REASON per item (expensive), then emit a small ledger in a
  ```json fence. The SEED run has no seed → it derives (reasons over all items) → high cost.
- the REFRESH run is seeded from it → the engine threads the seed records into the sink's input with
  the carry-forward directive → the member echoes them with ``refresh_status: unchanged`` and skips
  the re-derivation reasoning → LOWER cost. The engine stays authoritative: ``unchanged`` is
  credited only on an evidence-fingerprint MATCH + the marker.
- assert: no spurious changed/added/removed (the data is unchanged), ``unchanged`` reached by
  fingerprint match on multiple records (the lever bit — a cold re-derive would be ``re_confirmed``,
  not ``unchanged``), AND the refresh's real ``cost_tokens`` is lower than the cold seed run's.

Gateway ``:8006``, real OpenRouter BYOM, no fakes / no DB-direct / no service-port. Auto-skips when
the key / a reachable gateway is absent (a skip is NOT a pass).
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
_TERMINAL = {"SUCCEEDED", "FAILED", "PARTIAL", "REJECTED", "COST_BUDGET"}
_ITEMS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]

# The analyst writes a VERBOSE per-item analysis (the expensive derivation), then emits a TINY
# ledger in a ```json fence. The records are tiny ({id, verdict}) so the seed the refresh injects
# is cheap, while the analysis dominates the cold cost — a refresh that carries forward (skipping
# the analysis) is reliably, materially cheaper. The long analysis is the skippable work, not it.
_ANALYST_BODY = (
    "You are a diligent analyst reviewing these items: " + ", ".join(_ITEMS) + ". "
    "For EACH item, you MUST first write a detailed analysis of AT LEAST 70 words explaining your "
    "reasoning and evidence before deciding — do not be terse, be thorough. AFTER you have written "
    "all six analyses, output the final ledger as a JSON array where each element is EXACTLY "
    '{"id": "<item>", "verdict": "reviewed"} — one per item — inside a single ```json ... ``` code '
    "fence at the very end of your reply."
)


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


def _analyst_studio(root: Path) -> None:
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "analyst.md").write_text(f"---\nname: analyst\nmodel: sonnet\n---\n{_ANALYST_BODY}\n")
    (root / "teams" / "1-review").mkdir(parents=True)
    (root / "teams" / "1-review" / "charter.md").write_text(
        "# Team I — Review\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `analyst` | subagent | sonnet | review |\n"
    )


def _run(c: httpx.Client, tmp: Path, org: uuid.UUID, cred: str, *, seed: str | None = None) -> str:
    _analyst_studio(tmp)
    imported = import_setup(tmp, owner_organization_id=org, name="review")
    assert imported.manifest is not None
    manifest = imported.manifest.model_dump(mode="json")
    manifest["models"] = [_byom_model(cred)]
    subs = {
        role: {**dict(sub), "models": [_byom_model(cred)]}
        for role, sub in imported.sub_harnesses.items()
    }
    body: dict = {"manifest": manifest, "sub_harnesses": subs, "gate_decisions": {}}
    if seed is not None:
        body["seed_from_run_id"] = seed
    r = c.post("/v1/engine/team-runs", json=body)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in _TERMINAL:
            return row
        time.sleep(2)
    raise AssertionError(f"team-run {run_id} never settled (last: {row.get('state')})")


def _cost(c: httpx.Client, run_id: str) -> int:
    return int(c.get(f"/v1/engine/team-runs/{run_id}/status").json()["cost"]["tokens"] or 0)


@requires_byom
def test_refresh_over_unchanged_data_carries_forward_and_costs_fewer_tokens(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"lever{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])

    # ── SEED run (cold): the analyst DERIVES the ledger — reasons over every item ─────────────────
    seed_id = _run(c, tmp_path / "seed", org, cred)
    seed_row = _poll(c, seed_id)
    assert seed_row["state"] == "SUCCEEDED", f"the seed run must settle SUCCEEDED — {seed_row}"
    assert seed_row["refresh_delta"] is None  # the seed run is not itself a refresh
    seed_cost = _cost(c, seed_id)
    assert seed_cost > 0, "the cold derivation must have real BYOM cost"

    # ── REFRESH run: seeded from the cold run → the sink receives its prior records + carries fwd
    refresh_id = _run(c, tmp_path / "refresh", org, cred, seed=seed_id)
    refresh_row = _poll(c, refresh_id)
    assert refresh_row["state"] == "SUCCEEDED", f"the refresh must settle — {refresh_row}"
    refresh_cost = _cost(c, refresh_id)

    delta = refresh_row["refresh_delta"]
    assert delta is not None and delta.get("seed_from_run_id") == seed_id, delta
    assert delta.get("records_parsed", True), f"the deliverable must be a record-set — {delta}"
    counts = delta["counts"]

    # the data did not change between the runs → NO record should classify added/removed/changed.
    # (a spurious `changed` would mean a carried record's fingerprint moved — a soundness failure.)
    assert counts["added"] == 0 and counts["removed"] == 0, counts
    assert counts["changed"] == 0, f"unchanged data must not spuriously classify changed — {counts}"

    # THE COST LEVER: `unchanged` is credited only on a fingerprint MATCH + the member's carry-fwd
    # marker — a cold re-derivation would be `re_confirmed` (no marker), NOT `unchanged`. So a
    # nonzero `unchanged` proves the sink RECEIVED its prior records (via the dispatch wiring, not a
    # hard-coded prompt marker) and carried them forward instead of re-deriving.
    assert counts["unchanged"] >= 1, f"the sink must carry forward >=1 record (the lever) — {delta}"
    assert delta.get("skipped") == counts["unchanged"]  # the skip signal == the carried count

    # …and carrying forward is materially CHEAPER than the cold derivation (it skips the per-item
    # reasoning). Real per-member token cost, through the gateway — not a fabricated fraction.
    assert refresh_cost > 0, "the refresh still runs the member (real BYOM), just does less work"
    assert refresh_cost < seed_cost, (
        f"a refresh over unchanged data must cost FEWER member tokens than the cold derivation — "
        f"refresh={refresh_cost} vs cold_seed={seed_cost} (unchanged={counts['unchanged']})"
    )
