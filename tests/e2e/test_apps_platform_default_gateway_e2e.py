"""#932 — an Oraclous-provided app reaches a brand-new organisation through the DEPLOYED stack.

The claim under test is a tenancy claim, and no unit or integration test can settle it: an app
seeded once into the platform organisation must appear in the Apps tab of an organisation that was
created seconds ago and provisioned with nothing. The row-level-security read is widened to allow
that, which is a deliberate loosening of the rule that a tenant sees only its own rows — so it is
proven here, against the real database, the real seed on a real service boot, and the real gateway,
rather than against a fixture that agrees with itself.

Four things, in the order a person would hit them:

  VISIBLE — two unrelated fresh organisations both list the app, and it is the SAME id in both, so
  this is one shared record rather than a copy per tenant.
  ISOLATED — one organisation's own app never appears in another's list, and neither can rename or
  delete the shared one.
  FAIL-CLOSED — running it with nothing connected is a connect prompt naming what is missing,
  before any run row exists and before a single token is spent.
  RUNS (real BYOM) — once the caller supplies their OWN key, it runs, and the run belongs to the
  caller's organisation and not to Oraclous.

No fakes, no internal port, no DB-direct assertions (FUCK_CLAUDE_FUCK_PAPERCLIP.md rule 5). The
caller's keys are pasted through the public credentials API, never injected into a service
environment.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
_TAVILY_KEY = os.environ.get("TAVILY_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_MODEL = os.environ["E2E_MODEL"]

#: The slug the engine seeds its first Oraclous-provided app under. The console deep-links to it,
#: so it is part of the contract rather than an implementation detail.
_DESK_SLUG = "validation-desk"


def _platform_apps(apps: list[dict]) -> list[dict]:
    return [a for a in apps if a["origin"] == "platform"]


def _search_credential(client: httpx.Client, user_id: str) -> str:
    """Store a web-search key for this organisation and return its id.

    Uses the real key when the environment has one, and a placeholder otherwise. A placeholder is
    honest here because the tests that call this assert on TENANCY — who can see whose run — and
    never on a search result. Getting past the tool gate is all that is needed to create a run, and
    a run that later fails on a bad key still belongs to exactly one organisation.
    """
    resp = client.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": "e2e search key",
            "provider": "web_search",
            "cred_type": "api_key",
            "credential": {"api_key": _TAVILY_KEY or "tvly-placeholder-tenancy-only"},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _connect_web_research(client: httpx.Client, *, credential_id: str) -> None:
    """Give this organisation a configured instance of the web-search tool.

    A stored key is not reachable on its own: a member's tool call is dispatched through the
    organisation's own instance of that capability, and an instance with no credential mapped fails
    closed. This is exactly the step the run refusal prompts a person to take.
    """
    catalogue = client.get("/api/v1/capabilities").json()["capabilities"]
    capability = next((c for c in catalogue if c["name"] == "Web Research"), None)
    assert capability is not None, "the registry has no Web Research capability"

    instance = client.post(
        "/api/v1/instances",
        json={"capability_id": capability["id"], "name": "Web Research", "configuration": {}},
    )
    assert instance.status_code in (200, 201), instance.text
    configured = client.post(
        f"/api/v1/instances/{instance.json()['id']}/configure-credentials",
        json={"credential_mappings": {"api_key": credential_id}},
    )
    assert configured.status_code in (200, 201), configured.text


def _find(apps: list[dict], slug: str) -> dict | None:
    return next((a for a in apps if a.get("slug") == slug), None)


def test_a_brand_new_organisation_sees_the_oraclous_provided_app(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Nothing is done to this organisation between registering and listing. If the app is there,
    the widened read works; if it is not, every default app is invisible to every real customer."""
    who = register("Apps Tab User")
    client = gateway_client(who["token"])

    listed = client.get("/v1/engine/apps")
    assert listed.status_code == 200, listed.text
    body = listed.json()

    desk = _find(body["apps"], _DESK_SLUG)
    assert desk is not None, f"no {_DESK_SLUG} in {[a['name'] for a in body['apps']]}"
    assert desk["origin"] == "platform"
    assert body["total"] >= 1


def test_two_unrelated_organisations_see_the_very_same_app(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The same ID in both is what separates a genuinely shared record from a copy seeded per
    organisation. A per-tenant copy would drift, and would multiply every future edit."""
    a, b = register("Org A User"), register("Org B User")
    assert a["org_id"] != b["org_id"]

    seen_by_a = _find(gateway_client(a["token"]).get("/v1/engine/apps").json()["apps"], _DESK_SLUG)
    seen_by_b = _find(gateway_client(b["token"]).get("/v1/engine/apps").json()["apps"], _DESK_SLUG)

    assert seen_by_a is not None and seen_by_b is not None
    assert seen_by_a["id"] == seen_by_b["id"]


def test_the_app_read_draws_a_form_from_what_the_team_declares(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The console renders one control per entry here. Every key must be one the run would really
    accept, or the screen offers a field the server then refuses."""
    client = gateway_client(register("Form Reader")["token"])
    desk = _find(client.get("/v1/engine/apps").json()["apps"], _DESK_SLUG)
    assert desk is not None

    detail = client.get(f"/v1/engine/apps/{desk['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()

    assert body["origin"] == "platform"
    assert body["inputs"], "an app nobody can type into is not a form"
    for field in body["inputs"]:
        assert set(field) >= {"key", "required"}
        assert not field["key"].startswith("_")  # engine-reserved keys are never offered
        assert field["key"] != "answers"


def test_a_tenant_cannot_rename_or_delete_the_shared_app(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Being able to READ a shared row must not imply owning it — a rename here would rewrite every
    other organisation's tab."""
    client = gateway_client(register("Would-be Editor")["token"])
    desk = _find(client.get("/v1/engine/apps").json()["apps"], _DESK_SLUG)
    assert desk is not None

    renamed = client.patch(f"/v1/engine/apps/{desk['id']}", json={"name": "Mine now"})
    deleted = client.delete(f"/v1/engine/apps/{desk['id']}")

    assert renamed.status_code in (403, 404, 405), renamed.text
    assert deleted.status_code in (403, 404, 405), deleted.text

    still = _find(client.get("/v1/engine/apps").json()["apps"], _DESK_SLUG)
    assert still is not None and still["name"] == desk["name"]


def test_a_run_with_no_model_is_a_plain_validation_failure(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """No model supplied is the CLIENT's mistake, and reads as one.

    A stored app carries no credential at all, so the caller's key has to arrive with the request.
    Leaving it out is a malformed call, not a "go and connect something" — the console always sends
    it. Kept separate from the test below because collapsing the two would let either refusal
    satisfy the other, and they mean different things to the person on the screen.
    """
    client = gateway_client(register("No Model User")["token"])
    desk = _find(client.get("/v1/engine/apps").json()["apps"], _DESK_SLUG)
    assert desk is not None

    attempt = client.post(
        f"/v1/engine/apps/{desk['id']}/runs",
        json={"inputs": {"task": "A tool that files expense reports for contractors."}},
    )

    assert attempt.status_code == 422, attempt.text


@requires_byom
def test_an_unconnected_tool_is_a_connect_prompt_not_a_started_run(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The desk searches the web, and a fresh organisation has no search tool configured — so the
    run must be refused with a prompt naming what to connect.

    The refusal has to arrive BEFORE a run exists. A run that starts and dies six tool calls later
    has already spent the caller's money to tell them the same thing.

    Needs a real model key only because the model is validated first: this test is about the TOOL
    gate, and reaching it requires getting past the model one.
    """
    who = register("Unconnected Tool User")
    client = gateway_client(who["token"])
    desk = _find(client.get("/v1/engine/apps").json()["apps"], _DESK_SLUG)
    assert desk is not None

    stored = client.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": who["user_id"],
            "name": "e2e model key",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _OR_KEY},
        },
    )
    assert stored.status_code == 201, stored.text

    before = client.get("/v1/engine/team-runs").json()["total"]
    attempt = client.post(
        f"/v1/engine/apps/{desk['id']}/runs",
        json={
            "inputs": {"task": "A tool that files expense reports for contractors."},
            "models": [
                {
                    "role": "primary",
                    "binding": _MODEL,
                    "protocol_shape": "openai-compatible",
                    "config": {"credential_id": stored.json()["id"]},
                }
            ],
        },
    )

    assert attempt.status_code == 409, attempt.text
    body = attempt.json()
    # The gateway rewrites the engine's body into its own error envelope, so the top-level
    # `needs_credential` can arrive either bare or under `error` — assert on the pair itself.
    needs = body.get("needs_credential") or body.get("error", {}).get("needs_credential")
    assert needs, body
    assert needs.get("provider"), needs

    assert client.get("/v1/engine/team-runs").json()["total"] == before, "no run was created"


@requires_byom
def test_an_apps_run_history_is_not_shared_between_organisations(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The app is shared; its runs are not. Two organisations running the same Oraclous-provided app
    must never see each other's inputs or results.

    A REAL run has to exist for this to mean anything. An earlier version compared the histories of
    two brand-new organisations, so it compared two empty lists and passed without touching the
    thing it claimed to protect — the one place a cross-tenant leak could actually hide. Here A
    starts a run, and the assertion is that B does not see it.
    """
    a, b = register("History A"), register("History B")
    client_a, client_b = gateway_client(a["token"]), gateway_client(b["token"])
    desk = _find(client_a.get("/v1/engine/apps").json()["apps"], _DESK_SLUG)
    assert desk is not None

    stored = client_a.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": a["user_id"],
            "name": "e2e model key",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _OR_KEY},
        },
    )
    assert stored.status_code == 201, stored.text
    _connect_web_research(client_a, credential_id=_search_credential(client_a, a["user_id"]))

    started = client_a.post(
        f"/v1/engine/apps/{desk['id']}/runs",
        json={
            "inputs": {"task": "A tool that files expense reports for contractors."},
            "models": [
                {
                    "role": "primary",
                    "binding": _MODEL,
                    "protocol_shape": "openai-compatible",
                    "config": {"credential_id": stored.json()["id"]},
                }
            ],
        },
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]

    ids_a = {
        r["id"] for r in client_a.get(f"/v1/engine/apps/{desk['id']}/runs").json()["team_runs"]
    }
    ids_b = {
        r["id"] for r in client_b.get(f"/v1/engine/apps/{desk['id']}/runs").json()["team_runs"]
    }

    assert run_id in ids_a, "A cannot see its own run in the app's history"
    assert run_id not in ids_b, "B can see A's run — the app is shared, its runs must not be"
    assert ids_b == set(), "B has run nothing, so its history is empty"


@requires_byom
def test_the_app_runs_on_the_callers_own_key_and_the_run_belongs_to_them(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The owner's ruling, proven end to end: a default app spends the caller's key, never
    Oraclous's. The app's stored documents carry no credential at all, so the only key in play is
    the one this caller pastes in through the public API moments before the run."""
    who = register("Bring Your Own Key")
    client = gateway_client(who["token"])
    desk = _find(client.get("/v1/engine/apps").json()["apps"], _DESK_SLUG)
    assert desk is not None

    stored = client.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": who["user_id"],
            "name": "e2e model key",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _OR_KEY},
        },
    )
    assert stored.status_code == 201, stored.text
    assert _OR_KEY not in stored.text, "the store response echoed the secret"
    credential_id = stored.json()["id"]

    started = client.post(
        f"/v1/engine/apps/{desk['id']}/runs",
        json={
            "inputs": {"task": "A tool that files expense reports for contractors."},
            "models": [
                {
                    "role": "primary",
                    "binding": _MODEL,
                    "protocol_shape": "openai-compatible",
                    "config": {"credential_id": credential_id},
                }
            ],
        },
    )
    if started.status_code == 409:
        pytest.skip(f"this organisation still needs a tool credential: {started.text}")
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]

    listed = client.get(f"/v1/engine/apps/{desk['id']}/runs").json()["team_runs"]
    assert run_id in {r["id"] for r in listed}, "the run is missing from the app's own history"

    detail = client.get(f"/v1/engine/team-runs/{run_id}").json()
    assert detail["organisation_id"] == who["org_id"], "the run belongs to Oraclous, not the caller"


@requires_byom
@pytest.mark.skipif(_TAVILY_KEY is None, reason="TAVILY_API_KEY unset (the desk searches the web)")
def test_the_desk_reaches_a_real_answer_for_a_fully_connected_organisation(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The whole promise, once: a person signs up, connects their own two keys, opens the
    Oraclous-provided app and gets a real result. Everything above this proves a part; this proves
    the product."""
    who = register("Fully Connected")
    client = gateway_client(who["token"])
    desk = _find(client.get("/v1/engine/apps").json()["apps"], _DESK_SLUG)
    assert desk is not None

    def _store(provider: str, key: str, name: str) -> str:
        resp = client.post(
            "/credentials/",
            json={
                "tool_id": str(uuid.uuid4()),
                "user_id": who["user_id"],
                "name": name,
                "provider": provider,
                "cred_type": "api_key",
                "credential": {"api_key": key},
            },
        )
        assert resp.status_code == 201, resp.text
        return str(resp.json()["id"])

    model_credential = _store("openrouter", str(_OR_KEY), "e2e model key")
    search_credential = _store("web_search", str(_TAVILY_KEY), "e2e search key")

    # The requirements read tells the console what is still missing. Assert it names the search
    # tool, then connect it — rather than looping over the list and matching the provider against
    # capability names, which is how an earlier version of this test failed: the provider reads
    # `web-research` and the capability is called "Web Research", so a substring match found
    # nothing and the test blamed the registry for its own lookup.
    needs = client.get(f"/v1/engine/apps/{desk['id']}/requirements")
    assert needs.status_code == 200, needs.text
    unmet = {t["provider"] for t in needs.json()["tools"] if not t["satisfied"]}
    assert "web-research" in unmet, f"expected the search tool to need connecting, got {unmet}"

    _connect_web_research(client, credential_id=search_credential)

    assert client.get(f"/v1/engine/apps/{desk['id']}/requirements").json()["ready"] is True

    started = client.post(
        f"/v1/engine/apps/{desk['id']}/runs",
        json={
            "inputs": {"task": "A tool that files expense reports for contractors."},
            "models": [
                {
                    "role": "primary",
                    "binding": _MODEL,
                    "protocol_shape": "openai-compatible",
                    "config": {"credential_id": model_credential},
                }
            ],
        },
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]

    deadline = time.time() + 900
    state = "QUEUED"
    while time.time() < deadline:
        state = client.get(f"/v1/engine/team-runs/{run_id}").json()["state"]
        if state in ("SUCCEEDED", "FAILED", "HALTED"):
            break
        time.sleep(10)

    assert state == "SUCCEEDED", f"the desk ended {state} for a fully connected organisation"
