#!/usr/bin/env python
"""Register the validation desk's research team as a team draft (#851).

Reads the committed manifest under ``scripts/desk_research_team/``, binds the caller's own
organisation into it, and POSTs (or, with ``--draft-id``, PUTs) it to the deployed stack's
application-gateway as a team draft. Prints the draft id — the value the desk's
``VITE_DESK_TEAM_DRAFT_ID`` points at.

This script never runs the team — creating/replacing a draft costs nothing and calls no model
(``POST /v1/engine/team-drafts`` only validates and persists documents). Executing the team for
real is a separate, deliberate step (``POST /v1/engine/team-runs``) and is NOT part of this script.

Usage:
    uv run python scripts/register_desk_team.py --token <bearer> [--gateway-url http://localhost:8006]
    uv run python scripts/register_desk_team.py --register "Desk Team Owner"  # a fresh user
    uv run python scripts/register_desk_team.py --token <bearer> --draft-id <uuid>   # replace (PUT)
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from desk_research_team.build import build_documents  # noqa: E402


def _register(gateway_url: str, full_name: str) -> dict:
    email = f"desk-team-{uuid.uuid4().hex[:12]}@studio.test"
    with httpx.Client(base_url=gateway_url, timeout=15.0, trust_env=False) as c:
        reg = c.post(
            "/v1/auth/register",
            json={"email": email, "password": "TestPass123", "full_name": full_name},
        )
        reg.raise_for_status()
        token = reg.json()["access_token"]
        me = c.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    return {"token": token, "org_id": me["organisation_id"], "email": email}


def main() -> int:
    parser = argparse.ArgumentParser(description="Register the desk research team as a draft.")
    parser.add_argument("--gateway-url", default="http://localhost:8006")
    parser.add_argument("--token", default=None, help="bearer token of the owning org")
    parser.add_argument("--register", default=None, metavar="FULL_NAME", help="create a fresh user")
    parser.add_argument("--draft-id", default=None, help="replace (PUT) this existing draft id")
    parser.add_argument("--name", default="Desk Research Team")
    args = parser.parse_args()

    if args.token:
        token = args.token
        with httpx.Client(base_url=args.gateway_url, timeout=15.0, trust_env=False) as c:
            me = c.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
        org_id = me["organisation_id"]
    elif args.register:
        who = _register(args.gateway_url, args.register)
        token, org_id = who["token"], who["org_id"]
        print(f"registered {who['email']} (org {org_id})")
    else:
        parser.error("pass --token <bearer> or --register <full name>")
        return 2

    manifest, sub_harnesses = build_documents(uuid.UUID(org_id))
    body = {"name": args.name, "manifest": manifest, "sub_harnesses": sub_harnesses}

    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(
        base_url=args.gateway_url, headers=headers, timeout=30.0, trust_env=False
    ) as c:
        if args.draft_id:
            resp = c.put(f"/v1/engine/team-drafts/{args.draft_id}", json=body)
        else:
            resp = c.post("/v1/engine/team-drafts", json=body)

    if resp.status_code not in (200, 201):
        print(f"FAILED ({resp.status_code}): {resp.text}", file=sys.stderr)
        return 1

    envelope = resp.json()
    draft = envelope["draft"]
    print(f"draft id: {draft['id']}")
    print(f"org id:   {org_id}")
    print(f"version:  {draft['version']}")
    print(f"would_block: {envelope['would_block']}")
    if envelope["blocking"]:
        print("blocking:")
        for b in envelope["blocking"]:
            print(f"  - {b}")
    print()
    print(envelope["report"])
    print()
    print(json.dumps({"draft_id": draft["id"], "org_id": org_id}))
    return 1 if envelope["would_block"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
