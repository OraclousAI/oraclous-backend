#!/usr/bin/env python
"""Book M1 acceptance GO (ORAA E3 §22) — import a real team dir and run it on a LIVE stack.

This is the human "press GO" tool for the bound acceptance run (the §22 sign-off): point it at a
real ``.claude/agents`` team directory (e.g. the book studio) and it

  1. imports it (E2 ``import_setup``) into a Team Harness + its per-member sub-harnesses, printing
     the O8 dry-run report (and refusing to GO if the import has blocking flags),
  2. POSTs it to ``/v1/engine/team-runs`` (202 — the worker drives it; a 30-agent team won't block),
  3. polls ``GET`` and, at each PAUSE, shows the blocking human gate(s) for the author to advance
     (``--auto-approve`` approves everything for a smoke GO; otherwise it prompts).

It is the run-evidence harness for the use-case-guardian; the human still presses GO + signs off.
Requires the live stack reachable (``scripts/stack-up.sh``).

Usage:
    uv run python scripts/book_acceptance_go.py <team-dir> --org <uuid> \
        [--engine-url http://localhost:8085] [--token <bearer>] [--auto-approve]
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid

import httpx
from oraclous_ohm.import_.setup import import_setup, render_report


def _prompt_gates(gates: list[str]) -> dict[str, str]:
    decisions: dict[str, str] = {}
    for gate in gates:
        answer = input(f"  gate '{gate}' — approve / reject? ").strip().lower()
        decisions[gate] = "approve" if answer.startswith("a") else "reject"
    return decisions


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a team dir and GO on the live engine.")
    parser.add_argument("team_dir", help="path to a .claude/agents team directory")
    parser.add_argument("--org", required=True, help="owner organisation id (uuid)")
    parser.add_argument("--engine-url", default="http://localhost:8085")
    parser.add_argument("--token", default=None, help="bearer token for the engine")
    parser.add_argument("--auto-approve", action="store_true", help="approve every gate (smoke GO)")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()

    org = uuid.UUID(args.org)
    imported = import_setup(args.team_dir, owner_organization_id=org)
    print(render_report(imported.report))
    if imported.manifest is None or imported.report.would_block:
        print("\nBLOCKED — resolve the blocking flags above before GO.")
        return 1

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    body = {
        "manifest": imported.manifest.model_dump(mode="json"),
        "sub_harnesses": imported.sub_harnesses,
        "gate_decisions": {},
    }
    with httpx.Client(base_url=args.engine_url, headers=headers, timeout=30.0) as client:
        resp = client.post("/v1/engine/team-runs", json=body)
        resp.raise_for_status()
        run = resp.json()
        run_id = run["id"]
        print(f"\nGO — team run {run_id} accepted ({resp.status_code} {run['state']})")

        while True:
            time.sleep(args.poll_seconds)
            run = client.get(f"/v1/engine/team-runs/{run_id}").json()
            state = run["state"]
            if state in ("SUCCEEDED", "FAILED", "REJECTED"):
                print(f"-> {state}")
                return 0 if state == "SUCCEEDED" else 2
            if state == "PAUSED":
                gates = run["paused_at"]
                print(f"PAUSED at gate(s): {gates}")
                decisions = (
                    {g: "approve" for g in gates} if args.auto_approve else _prompt_gates(gates)
                )
                client.post(
                    f"/v1/engine/team-runs/{run_id}/advance", json={"gate_decisions": decisions}
                )
                print(f"advanced: {decisions}")


if __name__ == "__main__":
    sys.exit(main())
