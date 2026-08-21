#!/usr/bin/env python
"""Run the validation desk's research team and read the brief back the way the desk does (#851).

This is the acceptance proof, not a convenience: it drives the DEPLOYED stack through the
application-gateway on a real model, and then replays the desk's own read of the result rather
than asserting against the database.

The launch mirrors the desk exactly. The desk holds a team-draft id, reads that draft once, and
runs the documents the read returned — so this reads the same draft and posts the same documents.
Nothing is injected: the model and web-search keys were pasted through the credentials API when
the draft was registered, and the run carries only the user's task.

The read mirrors the desk too. It lists the run's artifacts, sorts them newest first, opens at most
six, and takes the first that parses as a brief — a document carrying both ``posture`` and
``headline``, which is how the desk tells a synthesis apart from a captured source.

Usage:
    uv run python scripts/run_desk_team.py --token <bearer> --draft-id <uuid> --task "..."
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

_TERMINAL = {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED", "COST_BUDGET"}
_PROBE_LIMIT = 6  # the desk opens at most six documents looking for the brief


def _parse_brief(content: str) -> dict[str, Any] | None:
    """The desk's tolerant parser: the whole content is the JSON, or it sits in the first fenced
    block of a written page. A document without both required fields is not a brief."""
    candidates = [content]
    if "```" in content:
        after = content.split("```", 1)[1]
        body = after.split("\n", 1)[1] if "\n" in after else ""
        candidates.append(body.split("```", 1)[0])
    for candidate in candidates:
        try:
            doc = json.loads(candidate.strip())
        except (ValueError, IndexError):
            continue
        if isinstance(doc, dict) and doc.get("posture") and doc.get("headline"):
            return doc
    return None


def _poll(client: httpx.Client, run_id: str, *, tries: int, every: float) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for _ in range(tries):
        row = client.get(f"/v1/engine/team-runs/{run_id}").json()
        if row.get("state") in _TERMINAL:
            return row
        time.sleep(every)
    raise SystemExit(f"run {run_id} never reached a terminal state (last: {row.get('state')})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the desk research team and read its brief.")
    parser.add_argument("--gateway-url", default="http://localhost:8006")
    parser.add_argument("--token", required=True, help="bearer token of the owning org")
    parser.add_argument("--draft-id", required=True, help="the draft the desk is configured with")
    parser.add_argument("--task", required=True, help="the idea or decision being validated")
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--poll-tries", type=int, default=240)
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"}
    with httpx.Client(
        base_url=args.gateway_url, headers=headers, timeout=60.0, trust_env=False
    ) as c:
        draft = c.get(f"/v1/engine/team-drafts/{args.draft_id}")
        if draft.status_code != 200:
            print(f"reading the draft failed ({draft.status_code})", file=sys.stderr)
            return 1
        documents = draft.json()["draft"]

        graph_id = c.post("/api/v1/graphs", json={"name": "desk-run"}).json()["id"]
        print(f"graph id: {graph_id}")

        created = c.post(
            "/v1/engine/team-runs",
            json={
                "manifest": documents["manifest"],
                "sub_harnesses": documents["sub_harnesses"],
                "gate_decisions": {},
                "graph_id": graph_id,
                "inputs": {"task": args.task},
            },
        )
        if created.status_code != 202:
            print(
                f"starting the run failed ({created.status_code}): {created.text}", file=sys.stderr
            )
            return 1
        run_id = created.json()["id"]
        print(f"run id:   {run_id}")

        done = _poll(c, run_id, tries=args.poll_tries, every=args.poll_seconds)
        print(f"state:    {done['state']}")
        print(f"members:  {json.dumps(done.get('member_status') or {})}")

        # the desk's read, replayed: newest first, at most six opened, first parseable wins
        listing = c.get("/v1/artifacts", params={"graph_id": graph_id, "team_run_id": run_id})
        artifacts = sorted(
            listing.json(), key=lambda a: str(a.get("created_at") or ""), reverse=True
        )
        print(f"artifacts: {len(artifacts)} on this run")
        for summary in artifacts[:_PROBE_LIMIT]:
            detail = c.get(f"/v1/artifacts/{summary['id']}").json()
            brief = _parse_brief(detail.get("content") or "")
            if brief is not None:
                print(f"\nthe desk reads this as the brief: artifact {summary['id']}")
                print(json.dumps(brief, indent=2))
                return 0
        print(
            f"\nNo brief. {min(len(artifacts), _PROBE_LIMIT)} documents were opened and none "
            "carried both a posture and a headline — this is the empty state the desk shows.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
