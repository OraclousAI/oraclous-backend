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

import os
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.security]

_REF = "core/knowledge-retriever@1.0.0"  # a bound capability ref (publish accepts any ref string)
_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")  # the user's BYOM key (for the live-invoke test)


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


def test_invoking_a_non_harness_binding_returns_a_clear_error(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Invoke requires the binding to be a runnable (kind=harness) capability. Binding to anything
    else is a clear 422 'not runnable' — NOT the opaque 'could not be parsed' it used to surface."""
    owner = gateway_client(register("Pub")["token"])
    slug = f"bad-{uuid.uuid4().hex[:8]}"
    owner.post("/v1/agents", json={"slug": slug, "bound_capability_ref": "not-a-harness-ref"})
    key = owner.post("/v1/integration-keys", json={"bound_agent_slug": slug}).json()["key"]
    resp = gateway_client(key).post(f"/v1/agents/{slug}/invoke", json={"input": "hi"})
    assert resp.status_code == 422, resp.text
    assert "not runnable" in resp.json()["error"]["message"]  # clear, actionable message


@pytest.mark.byom
@pytest.mark.skipif(not _MODEL_KEY, reason="OPENROUTER_API_KEY not set (the user's BYOM key)")
def test_a_user_publishes_a_real_agent_and_invokes_it_with_their_model(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The full real flow, all through the gateway, no mocks: the user stores their model token,
    registers a kind=harness capability whose OHM uses that model, publishes an agent bound to it,
    mints a key, and invokes it — getting a REAL OpenRouter completion (the per-run nonce echoed).
    Needs the LIVE harness (scripts/e2e.sh --byom)."""
    user = register("Agent Publisher")
    c = gateway_client(user["token"])
    org = c.get("/v1/auth/me").json()["organisation_id"]

    cred = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "my model",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _MODEL_KEY},
        },
    ).json()["id"]

    nonce = uuid.uuid4().hex[:10]
    ohm = {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": "real-agent", "owner_organization_id": org},
        "models": [
            {
                "role": "primary",
                "binding": "openrouter/openai/gpt-4o-mini",
                "protocol_shape": "openai-compatible",
                "config": {"credential_id": cred},
            }
        ],
        "actors": [{"role": "primary", "kind": "agent"}],
        "prompts": [
            {"role": "primary", "source": "inline", "body": f"Reply with exactly: {nonce}"}
        ],
        "runtime": {"entrypoint": "primary"},
    }
    cap_id = c.post(
        "/api/v1/capabilities", json={"name": "real-agent", "kind": "harness", "descriptor": ohm}
    ).json()["id"]

    slug = f"real-{uuid.uuid4().hex[:8]}"
    assert (
        c.post(
            "/v1/agents", json={"slug": slug, "bound_capability_ref": cap_id, "display_name": "R"}
        ).status_code
        == 201
    )
    key = c.post("/v1/integration-keys", json={"bound_agent_slug": slug}).json()["key"]

    resp = gateway_client(key).post(f"/v1/agents/{slug}/invoke", json={"input": "go"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded", body
    assert nonce in str(body.get("output") or ""), body  # a real LLM ran, via the published agent
