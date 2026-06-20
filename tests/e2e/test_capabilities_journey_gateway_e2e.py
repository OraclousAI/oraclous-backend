"""Capabilities/tools journey END-TO-END through the API GATEWAY — NO fakes.

A real user, through the gateway, discovers the seeded capabilities (the platform tools),
instantiates one, and attaches a stored credential to the instance — exercising the real
capability-registry and credential-broker. A tool instance is org-isolated. Nothing mocked.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def test_a_user_discovers_a_capability_instantiates_it_and_attaches_a_credential(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register("Cap User")
    c = gateway_client(user["token"])

    # discover the seeded capabilities (the user sees the platform tools)
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}
    assert "PostgreSQL Reader" in by_name, sorted(by_name)
    cap = by_name["PostgreSQL Reader"]

    # instantiate it for the org
    inst = c.post(
        "/api/v1/instances",
        json={"capability_id": cap["id"], "name": "my-pg", "configuration": {}, "settings": {}},
    )
    assert inst.status_code == 201, inst.text
    iid = inst.json()["id"]
    required = inst.json()["required_credentials"]
    assert required, "the instance should declare the credentials it needs"

    # store a credential and attach it to the instance (the configure-credentials mapping)
    cred = c.post(
        "/credentials/",
        json={
            "tool_id": cap["id"],
            "user_id": user["user_id"],
            "name": "pg",
            "provider": "postgresql",
            "cred_type": "raw",
            "credential": {"connection_string": "postgresql://x:y@db:5432/z"},
        },
    ).json()["id"]
    cfg = c.post(
        f"/api/v1/instances/{iid}/configure-credentials",
        json={"credential_mappings": {required[0]: cred}},
    )
    assert cfg.status_code == 200, cfg.text
    assert cfg.json()["credential_mappings"][required[0]] == cred


def test_a_tool_instance_is_org_isolated_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    owner = gateway_client(register("Cap A")["token"])
    cap_id = owner.get("/api/v1/capabilities").json()["capabilities"][0]["id"]
    iid = owner.post(
        "/api/v1/instances",
        json={"capability_id": cap_id, "name": "x", "configuration": {}, "settings": {}},
    ).json()["id"]
    other = gateway_client(register("Cap B")["token"])
    assert other.get(f"/api/v1/instances/{iid}").status_code in (403, 404)
