"""Published-agent journey END-TO-END through the API GATEWAY — NO fakes.

A real user publishes an agent and mints an integration key bound to it, then the public plane is
enforced through the gateway: the key reveals the agent's public metadata, an unauthenticated caller
is rejected at the edge, a key is bound to its own slug (cannot act on another), and a published
agent is org-isolated. Real application-gateway + integration-key store, nothing mocked.

(The agent's actual *execution* — running a model on the harness — is proven end-to-end by the BYOM
real-LLM e2e; a published agent additionally needs a resolvable harness manifest_ref to run, which
is a deeper registry concern.)
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.security]

_REF = "core/knowledge-retriever@1.0.0"  # a bound capability ref (publish accepts any ref string)


def test_publish_an_agent_mint_a_key_and_enforce_the_public_plane(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
    gateway_url: str,
) -> None:
    c = gateway_client(register("Publisher")["token"])
    slug = f"agent-{uuid.uuid4().hex[:8]}"

    pub = c.post(
        "/v1/agents", json={"slug": slug, "bound_capability_ref": _REF, "display_name": "A"}
    )
    assert pub.status_code == 201, pub.text
    assert any(a["slug"] == slug for a in c.get("/v1/agents").json())  # in the owner's list

    minted = c.post("/v1/integration-keys", json={"bound_agent_slug": slug})
    assert minted.status_code == 201, minted.text
    key = minted.json()["key"]

    # the integration key (a bearer token) reveals the agent's public metadata
    assert gateway_client(key).get(f"/v1/agents/{slug}").status_code == 200
    # an unauthenticated caller is rejected at the edge — for both the public read and the invoke
    assert httpx.get(f"{gateway_url}/v1/agents/{slug}", timeout=15).status_code == 401
    assert (
        httpx.post(
            f"{gateway_url}/v1/agents/{slug}/invoke", json={"input": "hi"}, timeout=15
        ).status_code
        == 401
    )


def test_an_integration_key_is_bound_to_its_own_agent_slug(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    c = gateway_client(register("Publisher2")["token"])
    a, b = f"a-{uuid.uuid4().hex[:8]}", f"b-{uuid.uuid4().hex[:8]}"
    for s in (a, b):
        c.post("/v1/agents", json={"slug": s, "bound_capability_ref": _REF})
    key_a = c.post("/v1/integration-keys", json={"bound_agent_slug": a}).json()["key"]

    kc = gateway_client(key_a)
    assert kc.get(f"/v1/agents/{a}").status_code == 200  # works for its own slug
    assert kc.get(f"/v1/agents/{b}").status_code in (401, 403)  # NOT another agent's slug


def test_a_published_agent_is_org_isolated_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    slug = f"iso-{uuid.uuid4().hex[:8]}"
    gateway_client(register("Pub A")["token"]).post(
        "/v1/agents", json={"slug": slug, "bound_capability_ref": _REF}
    )
    others = gateway_client(register("Pub B")["token"]).get("/v1/agents").json()
    assert all(a["slug"] != slug for a in others)  # B's org does not see A's published agent
