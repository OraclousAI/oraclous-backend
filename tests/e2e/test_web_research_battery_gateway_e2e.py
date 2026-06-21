"""Web-research battery DEPLOYED-STACK proof through the API GATEWAY — NO fakes (#486 / ADR-039).

A real user, through the gateway (:8006), discovers the seeded **Web Research** tool, instantiates
it, brings their OWN Tavily key via the credentials API (**BYOM** — the key source is
``TAVILY_API_KEY`` in deploy/.env, NEVER a registry server env), binds it, and dispatches a live
``search`` that returns REAL web hits. ``fetch``/``read`` run on the same key-mapped instance; an
unconfigured instance fails closed. Real capability-registry + credential-broker + a real Tavily
call — nothing mocked, no internal port, no DB-direct (FUCK_CLAUDE_FUCK_PAPERCLIP rule 5).

The package auto-skips when the gateway is down (conftest), and the keyed tests skip when the BYOM
source is unset — a skip is NOT a pass (rule 3), so a green run here means it really ran.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_TAVILY = os.environ.get("TAVILY_API_KEY", "")
_needs_key = pytest.mark.skipif(
    not _TAVILY, reason="TAVILY_API_KEY (BYOM source) not set — a skip is not a pass"
)


def _web_research_cap(c: httpx.Client) -> dict:
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}
    assert "Web Research" in by_name, f"web-research not seeded; got {sorted(by_name)}"
    return by_name["Web Research"]


def _instantiate(c: httpx.Client, cap_id: str) -> str:
    inst = c.post(
        "/api/v1/instances",
        json={"capability_id": cap_id, "name": "web-research", "configuration": {}, "settings": {}},
    )
    assert inst.status_code == 201, inst.text
    return inst.json()["id"]


def _store_and_bind_key(c: httpx.Client, cap_id: str, user_id: str, iid: str) -> str:
    cred = c.post(
        "/credentials/",
        json={
            "tool_id": cap_id,
            "user_id": user_id,
            "name": "my tavily key",
            "provider": "tavily",
            "cred_type": "api_key",
            "credential": {"api_key": _TAVILY},
        },
    )
    assert cred.status_code == 201, cred.text
    assert _TAVILY not in cred.text, "the BYOM secret must never be echoed by the store response"
    cid = cred.json()["id"]
    cfg = c.post(
        f"/api/v1/instances/{iid}/configure-credentials",
        json={"credential_mappings": {"api_key": cid}},
    )
    assert cfg.status_code == 200, cfg.text
    assert cfg.json()["credential_mappings"]["api_key"] == cid
    return cid


def test_unconfigured_web_research_search_fails_closed(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Key-gated: a search on an instance with no web-search key fails closed (no dispatch)."""
    user = register("WR Unconfigured")
    c = gateway_client(user["token"])
    cap = _web_research_cap(c)
    iid = _instantiate(c, cap["id"])

    ex = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "search", "query": "anything"}},
    )
    # required api_key is unmapped → ExecutionNotReadyError → 409; the tool never dispatches.
    assert ex.status_code == 409, ex.text
    assert "hits" not in ex.text


@_needs_key
def test_web_research_search_returns_real_hits_with_byom_key(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF: a user brings their Tavily key via the gateway and search returns REAL hits."""
    user = register("WR Searcher")
    c = gateway_client(user["token"])
    cap = _web_research_cap(c)
    iid = _instantiate(c, cap["id"])
    _store_and_bind_key(c, cap["id"], user["user_id"], iid)

    ex = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "search", "query": "Eurail global pass benefits"}},
    )
    assert ex.status_code == 201, ex.text
    out = ex.json()
    assert out["status"] == "SUCCESS", out
    hits = out["output_data"]["hits"]
    assert isinstance(hits, list) and len(hits) >= 1, out  # a real Tavily call returned web results
    assert all(h["url"].startswith("http") for h in hits), hits  # real web URLs, not a fake
    assert any(h["title"] for h in hits), hits
    assert out["output_data"] is not None and _TAVILY not in ex.text  # the key never leaks


@_needs_key
def test_web_research_fetch_and_read_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """fetch + read run through the gateway on a key-mapped instance (keyless in the connector)."""
    user = register("WR Reader")
    c = gateway_client(user["token"])
    cap = _web_research_cap(c)
    iid = _instantiate(c, cap["id"])
    _store_and_bind_key(c, cap["id"], user["user_id"], iid)

    fetched = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "fetch", "url": "https://example.com"}},
    )
    assert fetched.status_code == 201, fetched.text
    assert fetched.json()["status"] == "SUCCESS", fetched.text
    assert fetched.json()["output_data"]["content"], "fetch returned an empty body"

    read = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "read", "url": "https://example.com"}},
    )
    assert read.status_code == 201, read.text
    assert read.json()["status"] == "SUCCESS", read.text
    assert "Example Domain" in read.json()["output_data"]["text"]  # HTML stripped to readable text


@_needs_key
def test_web_research_blocks_ssrf_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """fetch refuses an internal/metadata target on the real stack (SSRF guard, fail-closed)."""
    user = register("WR SSRF")
    c = gateway_client(user["token"])
    cap = _web_research_cap(c)
    iid = _instantiate(c, cap["id"])
    _store_and_bind_key(c, cap["id"], user["user_id"], iid)

    ex = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={
            "input_data": {"operation": "fetch", "url": "http://169.254.169.254/latest/meta-data/"}
        },
    )
    assert ex.status_code == 201, (
        ex.text
    )  # the dispatch succeeds; the tool returns a structured fail
    out = ex.json()
    assert out["status"] == "FAILED" and out["error_type"] == "UNSAFE_URL", out
