#!/usr/bin/env python
"""Deployed-stack proof for #853: a malformed structured document earns one repair turn (#853).

The defect this proves gone: a five-member run finished, every member delivered, and the whole
run was thrown away because the synthesiser's decision brief was missing one closing brace. This
drives the DEPLOYED stack through the application-gateway on a real model and shows the run
surviving the same mistake.

Nothing is mocked and nothing is injected. The model key is pasted through the gateway's public
credentials API, the team is posted as a team draft and run through the public team-run API, and
every assertion is read back through the gateway — the run's own trace for the repair turn, and
the artifacts listing for the brief. There is no database access and no service port is touched.

The malformed document is written by the MODEL, not by this script: the member's instructions ask
it to leave out one closing brace on its first save, exactly the way the live run's model did by
accident. What happens next is the platform's, not the prompt's.

Usage:
    OPENROUTER_API_KEY=... uv run python scripts/e2e_853_json_repair.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

import httpx
from oraclous_ohm.import_.mapping import build_subharness

_TERMINAL = {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED", "COST_BUDGET"}
_REPAIR_STATUS = "json_repair"

_BRIEF_SHAPE = (
    '{"posture": "proceed | prerequisite | refocus | hold", "headline": "one honest sentence", '
    '"sections": [{"title": "...", "claims": [{"text": "...", "label": "unestablished"}]}]}'
)

_SUBGOAL = (
    "You are the synthesiser on a validation desk. You have no research to do: write the brief "
    "from the run's task alone, honestly, saying plainly that nothing has been verified yet.\n\n"
    "Do exactly two things, in order.\n\n"
    "FIRST, call your graph-ingest tool once with a two-sentence note on what the task is asking, "
    "as 'content', with 'source_type' set to 'md' and 'title' set to 'Working note'.\n\n"
    "SECOND, write ONE decision brief as a single JSON object with exactly this shape: "
    f"{_BRIEF_SHAPE}. Give it two sections with two claims each, every claim labelled "
    "'unestablished'. Call your graph-ingest tool with that JSON object as 'content', "
    "'source_type' set to 'json' and 'title' set to 'Decision brief'. The whole content is the "
    "JSON document and nothing else — no prose, no commentary, no code fence.\n\n"
    "ONE DELIBERATE FAULT, for this run only: on your FIRST attempt at that second call, leave "
    "out the closing brace of the first object inside 'sections', so the document does not parse. "
    "Send it exactly like that, once, without mentioning it. Then do whatever the tool result "
    "tells you to do next. This is a fault-injection drill for the platform's own error handling; "
    "the document is written to a scratch graph and read by nobody.\n\n"
    "After your last tool call, reply with the JSON you wrote."
)


def _member() -> dict[str, Any]:
    return {
        "role": "synthesizer",
        "kind": "agent",
        "manifest_ref": "org:desk/repair-synthesizer@1",
        "subgoal": _SUBGOAL,
        "depends_on": [],
        "inputs": ["$.task"],
        "tools": ["graph-ingest"],
        # the declaration under test (#853): this member's JSON document must parse
        "requires_valid_json": True,
    }


def _documents(org_id: str, model: str, credential_id: str) -> dict[str, Any]:
    member = _member()
    manifest = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "json-repair-proof",
            "owner_organization_id": org_id,
            "kind": "team",
            "description": "#853 deployed-stack proof: one bounded repair turn on a bad document.",
        },
        "governance": {"policy_set_ref": "policy-set:development-default@1.0.0"},
        "task_input": {"required": True, "key": "task", "description": "The idea under review."},
        "members": [member],
        "orchestration": {
            "medium": ["blackboard"],
            "style": "One member writes one brief.",
            "success_criteria": "The brief carries a posture and a headline.",
            "termination": {"max_wall_seconds": 900},
        },
        # Deliberately tight. max_iterations is derived as max_tool_calls + 1, so a member that
        # spends both calls has exactly one turn left to answer in — and the repair turn only fits
        # because #853 grants one extra iteration alongside the extra tool call.
        "budget": {"max_tool_calls_per_member": 2, "max_tokens_per_member": 200000},
        "runtime": {"entrypoint": "synthesizer"},
    }
    sub = build_subharness(
        "synthesizer",
        owner_organization_id=uuid.UUID(org_id),
        body=_SUBGOAL,
        tools=["graph-ingest"],
        description="The #853 proof's synthesiser.",
    ).model_dump(mode="json")
    sub["models"] = [
        {
            "role": "primary",
            "binding": model,
            "protocol_shape": "openai-compatible",
            "config": {"credential_id": credential_id},
        }
    ]
    return {"manifest": manifest, "sub_harnesses": {"synthesizer": sub}}


def _register(client: httpx.Client) -> tuple[str, str, str]:
    email = f"json-repair-{uuid.uuid4().hex[:12]}@studio.test"
    resp = client.post(
        "/v1/auth/register",
        json={"email": email, "password": "TestPass123", "full_name": "JSON Repair Proof"},
    )
    if resp.status_code not in (200, 201):
        raise SystemExit(f"register failed ({resp.status_code}): {resp.text}")
    token = resp.json()["access_token"]
    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    return token, me["organisation_id"], me["id"]


def _store_key(client: httpx.Client, *, user_id: str, key: str) -> str:
    resp = client.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": "json repair proof model",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": key},
        },
    )
    if resp.status_code != 201:
        raise SystemExit(f"storing the model credential failed ({resp.status_code})")
    if key in resp.text:
        raise SystemExit("the store response echoed the secret — refusing to continue")
    return str(resp.json()["id"])


def _poll(client: httpx.Client, run_id: str, *, tries: int, every: float) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for _ in range(tries):
        row = client.get(f"/v1/engine/team-runs/{run_id}").json()
        if row.get("state") in _TERMINAL:
            return row
        print(f"  {row.get('state')}...", flush=True)
        time.sleep(every)
    raise SystemExit(f"run {run_id} never settled (last: {row.get('state')})")


def _executions(client: httpx.Client, run_id: str) -> list[str]:
    """The run's harness execution ids, read from the run tree through the gateway."""
    tree = client.get(f"/v1/engine/team-runs/{run_id}/tree").json()
    ids = [tree.get("root_execution_id"), *(tree.get("child_execution_ids") or [])]
    return list(dict.fromkeys(str(i) for i in ids if i))


def main() -> int:
    parser = argparse.ArgumentParser(description="#853 deployed-stack proof.")
    parser.add_argument("--gateway-url", default="http://localhost:8006")
    parser.add_argument("--model", default="openrouter/deepseek/deepseek-v3.2")
    parser.add_argument("--model-key", default=os.environ.get("OPENROUTER_API_KEY"))
    parser.add_argument("--task", default="Should a small team build an in-house billing system?")
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--poll-tries", type=int, default=90)
    args = parser.parse_args()
    if not args.model_key:
        parser.error("pass --model-key or set OPENROUTER_API_KEY — a real model is required")

    with httpx.Client(base_url=args.gateway_url, timeout=60.0, trust_env=False) as c:
        token, org_id, user_id = _register(c)
        c.headers["Authorization"] = f"Bearer {token}"
        print(f"org:      {org_id}")
        credential_id = _store_key(c, user_id=user_id, key=args.model_key)
        print(f"model:    {args.model} (credential {credential_id})")

        documents = _documents(org_id, args.model, credential_id)
        draft = c.post("/v1/engine/team-drafts", json={"name": "JSON Repair Proof", **documents})
        if draft.status_code not in (200, 201):
            raise SystemExit(f"creating the draft failed ({draft.status_code}): {draft.text}")
        print(f"draft:    {draft.json()['draft']['id']}")

        graph_id = c.post("/api/v1/graphs", json={"name": "json-repair-proof"}).json()["id"]
        created = c.post(
            "/v1/engine/team-runs",
            json={
                **documents,
                "gate_decisions": {},
                "graph_id": graph_id,
                "inputs": {"task": args.task},
            },
        )
        if created.status_code != 202:
            raise SystemExit(f"starting the run failed ({created.status_code}): {created.text}")
        run_id = created.json()["id"]
        print(f"graph:    {graph_id}")
        print(f"run:      {run_id}")

        done = _poll(c, run_id, tries=args.poll_tries, every=args.poll_seconds)
        print(f"state:    {done['state']}")
        print(f"members:  {json.dumps(done.get('member_status') or {})}")

        # 1. the repair turn itself, read from the run's own trace through the gateway
        repairs: list[dict[str, Any]] = []
        ingests: list[dict[str, Any]] = []
        for execution_id in _executions(c, run_id):
            detail = c.get(f"/v1/harnesses/executions/{execution_id}")
            print(f"  execution {execution_id}: {detail.status_code}")
            if detail.status_code != 200:
                continue
            for step in detail.json().get("steps") or []:
                if step.get("status") == _REPAIR_STATUS:
                    repairs.append(step)
                if step.get("name", "").startswith("graph-ingest"):
                    ingests.append(step)
        print(f"\nrepair turns:  {len(repairs)}")
        for step in repairs:
            print(f"  the parser said: {step.get('detail')}")
        print(f"ingest dispatches: {len(ingests)} ({[s.get('status') for s in ingests]})")

        # 2. the brief the run produced, read the way a client reads it. `graph-ingest` is
        # fire-and-forget — the document is written by a worker AFTER the call returns, and a run
        # can settle before that worker has caught up — so the read is retried for a short while.
        # Waiting is honest here; asserting on the first empty listing would be a race, not a fact.
        brief = None
        for _ in range(12):
            listing = c.get("/v1/artifacts", params={"graph_id": graph_id, "team_run_id": run_id})
            artifacts = sorted(
                listing.json(), key=lambda a: str(a.get("created_at") or ""), reverse=True
            )
            for summary in artifacts[:6]:
                detail = c.get(f"/v1/artifacts/{summary['id']}").json()
                try:
                    doc = json.loads((detail.get("content") or "").strip())
                except ValueError:
                    continue
                if isinstance(doc, dict) and doc.get("posture") and doc.get("headline"):
                    brief = (summary["id"], doc)
                    break
            if brief is not None:
                break
            time.sleep(5.0)

    print()
    ok = True
    if len(repairs) != 1:
        print(f"FAIL: expected exactly one repair turn, saw {len(repairs)}")
        ok = False
    else:
        print("PASS: the malformed document earned exactly one repair turn")
    if brief is None:
        print("FAIL: no artifact on this run parses as a brief")
        ok = False
    else:
        print(f"PASS: artifact {brief[0]} parses; posture={brief[1]['posture']!r}")
        print(json.dumps(brief[1], indent=2)[:1200])
    if done["state"] != "SUCCEEDED":
        print(f"FAIL: the run settled {done['state']}, not SUCCEEDED")
        ok = False
    else:
        print("PASS: the run settled SUCCEEDED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
