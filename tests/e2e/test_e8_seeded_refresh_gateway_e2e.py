"""#602 (E8) — seeded-refresh: a refresh run emits a first-class 5-way what-changed delta, LIVE.

ADR-048 decision 3: a refresh run seeds a NAMED prior run's output and produces, beside the
deliverable, a 5-way delta — each record ``added | removed | changed | unchanged | re_confirmed`` —
computed engine-side at settle by comparing this run's records to the seed's (identity + a
per-record fingerprint). ``re_confirmed`` (re-examined, still true) is distinct from ``unchanged``,
Lock O3.

This proves the MECHANISM on the deployed stack (gateway ``:8006``, real OpenRouter BYOM, no fakes):
seed run N−1 emits a small JSON ledger → refresh run N (``seed_from_run_id`` = run N−1) emits a
modified ledger (a record removed, one changed, one added, the rest carried) → the engine settles
the run with a ``refresh_delta`` classifying every fresh record, keyed by record id + evidence
fingerprint. A bad ``seed_from_run_id`` is rejected 422 at create. The EURail ``--refresh-from``
exact reproduction is the CTO's RULE-4 (the ledger + imported agent-pack live only on the reviewer's
deployed env, not in this repo — per #597). Auto-skips without the BYOM key / a reachable gateway.
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

# All 5 classes exercised distinctly (incl. re_confirmed ≠ unchanged, Lock O3):
_SEED_LEDGER = (
    '[{"id":"a","fact":"alpha"},{"id":"b","fact":"bravo"},'
    '{"id":"c","fact":"charlie"},{"id":"d","fact":"delta"}]'
)
# vs seed: a carried WITH a skip marker → unchanged; b carried verbatim WITHOUT a marker →
# re_confirmed; c's fact revised → changed; d dropped → removed; e new → added.
_REFRESH_LEDGER = (
    '[{"id":"a","fact":"alpha","refresh_status":"unchanged"},'
    '{"id":"b","fact":"bravo"},'
    '{"id":"c","fact":"charlie-REVISED"},'
    '{"id":"e","fact":"echo"}]'
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


def _reporter_studio(root: Path, ledger: str) -> None:
    """A one-member 'reporter' team told to emit an EXACT JSON ledger (so the delta is real)."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    body = (
        "You are a data reporter. Output the following JSON array of records EXACTLY as given, "
        "verbatim, with no changes: "
        f"{ledger}\n\nReply with ONLY that JSON array — no prose, no fences, no explanation."
    )
    (agents / "reporter.md").write_text(f"---\nname: reporter\nmodel: sonnet\n---\n{body}\n")
    (root / "teams" / "1-report").mkdir(parents=True)
    (root / "teams" / "1-report" / "charter.md").write_text(
        "# Team I — Report\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `reporter` | subagent | sonnet | report |\n"
    )


def _run_team(
    c: httpx.Client, tmp: Path, org: uuid.UUID, cred: str, ledger: str, *, seed: str | None = None
) -> str:
    """Import a one-member reporter team told to emit ``ledger``, fire it (optionally as a refresh
    seeded from ``seed``), and return the team_run_id."""
    _reporter_studio(tmp, ledger)
    imported = import_setup(tmp, owner_organization_id=org, name="report")
    assert imported.manifest is not None
    manifest = imported.manifest.model_dump(mode="json")
    manifest["models"] = [_byom_model(cred)]
    sub_harnesses = {
        role: {**dict(sub), "models": [_byom_model(cred)]}
        for role, sub in imported.sub_harnesses.items()
    }
    body: dict = {"manifest": manifest, "sub_harnesses": sub_harnesses, "gate_decisions": {}}
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


@requires_byom
def test_seeded_refresh_emits_a_5way_what_changed_delta(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"refresh{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])

    # ── a bad seed_from_run_id is rejected 422 at create (fail-fast, never mid-drive) ────────────
    bad = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": {"ohm_version": "1.1", "metadata": {"kind": "team"}, "members": []},
            "seed_from_run_id": str(uuid.uuid4()),
        },
    )
    assert bad.status_code in (400, 422), bad.text

    # ── SEED run (N−1): emit the seed ledger, settle SUCCEEDED ───────────────────────────────────
    seed_id = _run_team(c, tmp_path / "seed", org, cred, _SEED_LEDGER)
    seed_row = _poll(c, seed_id)
    assert seed_row["state"] == "SUCCEEDED", f"the seed run must settle SUCCEEDED — {seed_row}"
    assert seed_row["refresh_delta"] is None  # the seed run is not itself a refresh

    # ── REFRESH run (N): seeded from N−1, emit the modified ledger ────────────────────────────────
    refresh_id = _run_team(c, tmp_path / "refresh", org, cred, _REFRESH_LEDGER, seed=seed_id)
    refresh_row = _poll(c, refresh_id)
    assert refresh_row["state"] == "SUCCEEDED", f"the refresh run must settle — {refresh_row}"

    # THE CONTRACT: a first-class 5-way delta, keyed to the seed run, engine-authoritative
    delta = refresh_row["refresh_delta"]
    assert delta is not None, f"a refresh run must emit a refresh_delta — {refresh_row}"
    assert delta.get("seed_from_run_id") == seed_id
    assert delta.get("records_parsed", True), f"the deliverable must be a record-set — {delta}"

    def _ids(cls: str) -> set[str]:
        return {r.get("id") for r in delta.get(cls, [])}

    # every class materialises with the RIGHT identities — a mislabel would fail here (the
    # classification is engine-authoritative from the fingerprint set-diff, not the member's claim).
    assert _ids("added") == {"e"}, f"added (fresh id absent from seed) — {delta.get('added')}"
    assert _ids("removed") == {"d"}, f"removed (seed id absent from fresh) — {delta.get('removed')}"
    assert _ids("changed") == {"c"}, f"changed (fingerprint moved) — {delta.get('changed')}"
    assert _ids("unchanged") == {"a"}, (
        f"unchanged (fp match + skip marker) — {delta.get('unchanged')}"
    )
    assert _ids("re_confirmed") == {"b"}, f"re_confirmed (fp match, NO marker) — {delta}"
    # re_confirmed ≠ unchanged preserved end-to-end (Lock O3, never silently worse): a verbatim-
    # carried record without a skip claim is re_confirmed (re-examined), never a false unchanged.
    counts = delta["counts"]
    assert counts["unchanged"] == 1 and counts["re_confirmed"] == 1, counts
    # the skip (cost) signal: exactly the fingerprint-match + marked record is credited skipped.
    assert delta.get("skipped") == 1, f"the carry-forward/skip signal — {delta}"
    # real BYOM work happened on both runs (a cost signal, not a fabricated fraction)
    assert int(seed_row.get("cost_tokens") or 0) > 0
    assert int(refresh_row.get("cost_tokens") or 0) > 0
