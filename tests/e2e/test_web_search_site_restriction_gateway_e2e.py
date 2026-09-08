"""#951 DEPLOYED-STACK proof through the API GATEWAY — a search restricted to named websites.

A real user, through the gateway (:8006), brings their own Tavily key via the credentials API
(**BYOM** — from ``TAVILY_API_KEY`` in deploy/.env.test, never a registry server env), and runs
searches that are restricted to websites they name. Real capability-registry, real credential
broker, a real Tavily call. Nothing mocked, no internal port, no DB-direct assertion
(FUCK_CLAUDE_FUCK_PAPERCLIP rule 5).

This is the only step that can prove #951 at all, for two reasons a green unit suite cannot cover:

* **The vendor silently ignores a full URL.** A probe on 2026-09-07 established that
  ``include_domains: ["https://theverge.com/tech"]`` comes back 200 with completely unrestricted
  results. A unit test asserts what we SEND; only a live call proves the vendor honoured it.
* **Tool descriptors are persisted at registration.** The argument and its description could be
  perfectly declared in the source and still never reach a running member, if the seeded row were
  not updated in place on restart.

The package auto-skips when the gateway is down (conftest), and the keyed tests skip when the BYOM
source is unset — a skip is NOT a pass (rule 3), so a green run here means it really ran.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_TAVILY = os.environ.get("TAVILY_API_KEY", "")
_needs_key = pytest.mark.skipif(
    not _TAVILY, reason="TAVILY_API_KEY (BYOM source) not set — a skip is not a pass"
)

#: Two sites with plenty of daily technology coverage, so a restricted search has something to find.
_SITES = ["theverge.com", "arstechnica.com"]
_QUERY = "artificial intelligence news this week"


def _host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _from_one_of(url: str, sites: list[str]) -> bool:
    """True when the hit's host IS one of the named sites, or a subdomain of one."""
    host = _host_of(url)
    return any(host == site or host.endswith(f".{site}") for site in sites)


def _search_ready_instance(
    c: httpx.Client, user_id: str
) -> str:  # → an instance id with the BYOM key bound
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}
    assert "Web Research" in by_name, f"web-research not seeded; got {sorted(by_name)}"
    cap_id = by_name["Web Research"]["id"]
    inst = c.post(
        "/api/v1/instances",
        json={"capability_id": cap_id, "name": "web-research", "configuration": {}, "settings": {}},
    )
    assert inst.status_code == 201, inst.text
    iid = inst.json()["id"]
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
    cfg = c.post(
        f"/api/v1/instances/{iid}/configure-credentials",
        json={"credential_mappings": {"api_key": cred.json()["id"]}},
    )
    assert cfg.status_code == 200, cfg.text
    return str(iid)


def _search(c: httpx.Client, iid: str, **args: object) -> dict:
    ex = c.post(
        f"/api/v1/instances/{iid}/execute",
        json={"input_data": {"operation": "search", "query": _QUERY, **args}},
    )
    assert ex.status_code == 201, ex.text
    return dict(ex.json())


@_needs_key
def test_a_restricted_search_returns_hits_only_from_the_named_sites(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF: the restriction reaches the vendor and the vendor honours it, live."""
    user = register(f"sites{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    iid = _search_ready_instance(c, user["user_id"])

    out = _search(c, iid, sites=_SITES)
    assert out["status"] == "SUCCESS", out
    hits = out["output_data"]["hits"]
    assert len(hits) >= 1, out  # a real Tavily call returned real web results
    off_site = [h["url"] for h in hits if not _from_one_of(h["url"], _SITES)]
    assert not off_site, f"results escaped the named sites: {off_site}"

    # D4b: the run says which hostnames it actually searched, so a person who supplied a
    # wrong-but-plausible address can see it. No mechanical check could catch that for them.
    assert out["output_data"]["searched_sites"] == _SITES, out["output_data"]


@_needs_key
def test_a_pasted_link_is_reduced_to_its_address_and_the_restriction_still_holds(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The case a green unit suite cannot prove. Sent raw, this exact value comes back 200 with
    UNRESTRICTED results — the vendor drops the restriction and says nothing. So this asserts the
    live hits really are confined, which is only true if the address was stripped before sending."""
    user = register(f"pasted{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    iid = _search_ready_instance(c, user["user_id"])

    out = _search(c, iid, sites=["https://www.theverge.com/tech"])
    assert out["status"] == "SUCCESS", out
    hits = out["output_data"]["hits"]
    assert len(hits) >= 1, out
    escaped = [h["url"] for h in hits if not _from_one_of(h["url"], ["theverge.com"])]
    assert not escaped, f"the pasted link was ignored by the vendor: {escaped}"
    # reported as the CLEANED hostname, never the text that was typed
    assert out["output_data"]["searched_sites"] == ["theverge.com"], out["output_data"]


@_needs_key
def test_several_sites_sent_as_one_string_are_split_rather_than_refused(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Ruled 2026-09-08. A member sending "theverge.com, bbc.co.uk" is asking for a restriction in
    the wrong container, and dropping that silently would be this issue's own bug in a new place.

    It lives here rather than only in the unit suite because the connector is not the first thing
    the value meets: the declared input schema is type-checked BEFORE the connector runs, so an
    ``array`` declaration refuses the string at the boundary and the splitting never happens. Only
    a call through the gateway crosses that layer."""
    user = register(f"onestring{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    iid = _search_ready_instance(c, user["user_id"])

    out = _search(c, iid, sites="theverge.com, arstechnica.com")
    assert out["status"] == "SUCCESS", out
    assert out["output_data"]["searched_sites"] == _SITES, out["output_data"]
    off_site = [h["url"] for h in out["output_data"]["hits"] if not _from_one_of(h["url"], _SITES)]
    assert not off_site, f"results escaped the named sites: {off_site}"


@_needs_key
def test_the_same_search_with_no_sites_still_ranges_over_the_whole_web(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The unchanged path: an existing app that names no sites behaves exactly as it did, and the
    result carries nothing new for it to have to understand."""
    user = register(f"nosites{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    iid = _search_ready_instance(c, user["user_id"])

    out = _search(c, iid)
    assert out["status"] == "SUCCESS", out
    assert len(out["output_data"]["hits"]) >= 1, out
    assert "searched_sites" not in out["output_data"], out["output_data"]
    assert "note" not in out["output_data"], out["output_data"]


@_needs_key
def test_a_site_named_by_its_publication_name_is_refused_and_told_which_value_was_wrong(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Ruling 3: a publication NAME is not an address, and guessing one would be indistinguishable
    from getting it right. So it is refused, and the refusal names the value that has to change."""
    user = register(f"byname{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    iid = _search_ready_instance(c, user["user_id"])

    out = _search(c, iid, sites=["BBC News"])
    assert out["status"] == "FAILED", out
    assert out["error_type"] == "INVALID_INPUT", out
    assert "BBC News" in (out["error_message"] or ""), out
    # never the vendor's own refusal body, which names the value too (ADR-008)
    assert "include_domains" not in (out["error_message"] or ""), out


@_needs_key
def test_the_running_stack_offers_a_member_the_new_argument_with_its_description(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Descriptors are persisted at registration, so source alone proves nothing: this reads the
    LIVE seeded row back through the gateway and checks the argument a member is offered."""
    user = register(f"descr{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    caps = c.get("/api/v1/capabilities").json()["capabilities"]
    by_name = {x["name"]: x for x in caps}

    for tool in ("Web Research", "WebSearch"):
        assert tool in by_name, f"{tool} not seeded; got {sorted(by_name)}"
        descriptor = c.get(f"/api/v1/capabilities/{by_name[tool]['id']}").json()["descriptor"]
        search = next(op for op in descriptor["spec"]["capabilities"] if op["name"] == "search")
        schema = search["parameters_schema"]
        assert schema["required"] == ["query"], schema  # naming sites stays optional
        sites = schema["properties"]["sites"]
        assert sites["type"] == "array" and sites["items"] == {"type": "string"}, sites
        description = sites["description"].lower()
        assert "address" in description or "hostname" in description, sites["description"]
        assert "theverge.com" in description or "bbc.co.uk" in description, sites["description"]
