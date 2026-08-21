#!/usr/bin/env python
"""Register the validation desk's research team as a team draft (#851).

Reads the committed manifest under ``scripts/desk_research_team/``, binds the caller's own
organisation into it, and POSTs (or, with ``--draft-id``, PUTs) it to the deployed stack's
application-gateway as a team draft. Prints the draft id — the value the desk's
``VITE_DESK_TEAM_DRAFT_ID`` points at.

The desk runs the documents this draft holds, unchanged, so the draft has to carry everything a
run needs. Two BYOM keys are pasted through the gateway's public credentials API first — never
injected into a service environment — and the model credential is bound onto every member:

* the model key (OpenRouter), bound as each sub-harness's ``models[0]``;
* the web-search key (Tavily), bound onto an organisation-wide instance of the Web Research tool.
  Storing the key alone is not enough: the tool is dispatched through a configured instance, and
  without one the two searching members fail closed with "no configured instance".

This script never runs the team — creating or replacing a draft calls no model
(``POST /v1/engine/team-drafts`` only validates and persists documents). Executing the team is
``scripts/run_desk_team.py``, a separate deliberate step.

Usage:
    uv run python scripts/register_desk_team.py --register "Desk Team Owner" \
        --model-key "$OPENROUTER_API_KEY" --search-key "$TAVILY_API_KEY"
    uv run python scripts/register_desk_team.py --token <bearer> --draft-id <uuid>   # replace (PUT)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from desk_research_team.build import build_documents  # noqa: E402


def _store_credential(
    client: httpx.Client, *, user_id: str, provider: str, key: str, name: str
) -> str:
    """Paste a BYOM key through the gateway's public credentials API and return its id.

    The key is never echoed back by the store response (KMS-sealed), which is asserted here rather
    than trusted — a script that prints a customer's key into a terminal is its own incident.
    """
    resp = client.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": name,
            "provider": provider,
            "cred_type": "api_key",
            "credential": {"api_key": key},
        },
    )
    if resp.status_code != 201:
        raise SystemExit(f"storing the {provider} credential failed ({resp.status_code})")
    if key in resp.text:
        raise SystemExit(f"the {provider} store response echoed the secret — refusing to continue")
    return str(resp.json()["id"])


def _configure_tool(client: httpx.Client, *, capability_name: str, credential_id: str) -> str:
    """Give the organisation a configured instance of a keyed tool, and return its id.

    A stored credential is not reachable on its own. A member's tool call is dispatched through the
    organisation's instance of that capability, and an instance with no credential mapped fails
    closed — which is what "the organisation has no configured instance of it" means when a member
    dies on its first search.
    """
    catalogue = client.get("/api/v1/capabilities").json()["capabilities"]
    capability = next((c for c in catalogue if c["name"] == capability_name), None)
    if capability is None:
        raise SystemExit(f"the registry has no capability named {capability_name!r}")
    instance = client.post(
        "/api/v1/instances",
        json={"capability_id": capability["id"], "name": capability_name, "configuration": {}},
    )
    if instance.status_code not in (200, 201):
        raise SystemExit(f"creating the {capability_name} instance failed ({instance.status_code})")
    instance_id = str(instance.json()["id"])
    configured = client.post(
        f"/api/v1/instances/{instance_id}/configure-credentials",
        json={"credential_mappings": {"api_key": credential_id}},
    )
    if configured.status_code not in (200, 201):
        raise SystemExit(f"configuring {capability_name} failed ({configured.status_code})")
    return instance_id


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
    return {
        "token": token,
        "org_id": me["organisation_id"],
        "user_id": me["id"],
        "email": email,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Register the desk research team as a draft.")
    parser.add_argument("--gateway-url", default="http://localhost:8006")
    parser.add_argument("--token", default=None, help="bearer token of the owning org")
    parser.add_argument("--register", default=None, metavar="FULL_NAME", help="create a fresh user")
    parser.add_argument("--draft-id", default=None, help="replace (PUT) this existing draft id")
    parser.add_argument("--name", default="Desk Research Team")
    parser.add_argument(
        "--model-key",
        default=os.environ.get("OPENROUTER_API_KEY"),
        help="the OpenRouter key every member's model runs on (default: $OPENROUTER_API_KEY)",
    )
    parser.add_argument(
        "--model",
        default="openrouter/deepseek/deepseek-v3.2",
        help="the model every member runs on; the provider prefix names the gateway that serves "
        "it, so an OpenRouter model reads openrouter/<vendor>/<model>",
    )
    parser.add_argument(
        "--search-key",
        default=os.environ.get("TAVILY_API_KEY"),
        help="the web-search key the searching members need (default: $TAVILY_API_KEY)",
    )
    args = parser.parse_args()

    if args.token:
        token = args.token
        with httpx.Client(base_url=args.gateway_url, timeout=15.0, trust_env=False) as c:
            me = c.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
        org_id, user_id = me["organisation_id"], me["id"]
    elif args.register:
        who = _register(args.gateway_url, args.register)
        token, org_id, user_id = who["token"], who["org_id"], who["user_id"]
        print(f"registered {who['email']} (org {org_id})")
    else:
        parser.error("pass --token <bearer> or --register <full name>")
        return 2

    if not args.model_key:
        parser.error(
            "pass --model-key (or set OPENROUTER_API_KEY) — a team with no model cannot run"
        )
        return 2

    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(
        base_url=args.gateway_url, headers=headers, timeout=30.0, trust_env=False
    ) as c:
        model_cred = _store_credential(
            c, user_id=user_id, provider="openrouter", key=args.model_key, name="desk team model"
        )
        print(f"model credential: {model_cred}")
        if args.search_key:
            search_cred = _store_credential(
                c,
                user_id=user_id,
                provider="web_search",
                key=args.search_key,
                name="desk team web search",
            )
            print(f"search credential: {search_cred}")
            search_tool = _configure_tool(
                c, capability_name="Web Research", credential_id=search_cred
            )
            print(f"web research tool: {search_tool}")
        else:
            print(
                "WARNING: no web-search key given. The researcher and the cross-examiner will "
                "fail closed on every search, and the brief will rest on nothing."
            )

        manifest, sub_harnesses = build_documents(uuid.UUID(org_id))
        model = {
            "role": "primary",
            "binding": args.model,
            "protocol_shape": "openai-compatible",
            "config": {"credential_id": model_cred},
        }
        for sub in sub_harnesses.values():
            sub["models"] = [model]
        body = {"name": args.name, "manifest": manifest, "sub_harnesses": sub_harnesses}

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
    print(json.dumps({"draft_id": draft["id"], "org_id": org_id, "token": token}))
    return 1 if envelope["would_block"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
