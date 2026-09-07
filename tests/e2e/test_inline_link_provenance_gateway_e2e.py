"""#944 — an inline link the run never fetched is FLAGGED, end to end through the gateway.

The reported bug, live: a member's free-text answer wrote ``[Source](https://www.okta.com/blog/…)``
and nothing in the pipeline ever read that URL, so the console rendered it as a real anchor and a
person clicked it trusting the run had fetched it (team run ``8ef18ab0``, the ``linker`` role).

This proves the check on the DEPLOYED stack, through the gateway only, with a real model and a real
web search — no fakes, no injected server-side state, nothing asserted against the database. The
user brings their own model key and their own search key through the public credentials API,
exactly as a person does.

**The shape is the reported one: some links real, one invented.** The member really searches the
web, so the URLs the search returned are verified; it is also told to cite one extra source it must
not look up, so that one is not. Under the #944 ruling that answer SHIPS, flagged — sending it back
would throw away real work over one bad link — and the run reports ``has_unverified_links`` with
the offending URL named per member. Instructing the member to write that URL is not a shortcut past
the check: how a URL gets into an answer is exactly what the platform cannot control, and the whole
point is that the platform catches it afterwards regardless.

Requires the harness LIVE and both keys (``scripts/e2e.sh --byom``). Auto-skips otherwise, and a
skip is NOT a pass.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")
_SEARCH_KEY = os.environ.get("TAVILY_API_KEY")
requires_keys = pytest.mark.skipif(
    _MODEL_KEY is None or _SEARCH_KEY is None,
    reason="OPENROUTER_API_KEY / TAVILY_API_KEY unset (real BYOM + real web search)",
)

# The URL the member is told to cite and told not to look up. A real host, an invented path — the
# exact shape of the reported fabrication, and nothing about it can be caught by inspection alone.
_INVENTED = "https://www.okta.com/blog/2023/10/okta-ai-token-costs"


def _store(c: httpx.Client, user_id: str, provider: str, key: str, name: str) -> str:
    resp = c.post(
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
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _connect_web_research(c: httpx.Client, credential_id: str) -> None:
    """Give this organisation a configured instance of the web-search tool, through the gateway."""
    catalogue = c.get("/api/v1/capabilities").json()["capabilities"]
    capability = next((x for x in catalogue if x["name"] == "Web Research"), None)
    assert capability is not None, "the registry has no Web Research capability"
    instance = c.post(
        "/api/v1/instances",
        json={"capability_id": capability["id"], "name": "Web Research", "configuration": {}},
    )
    assert instance.status_code == 201, instance.text
    mapped = c.post(
        f"/api/v1/instances/{instance.json()['id']}/credentials",
        json={"credential_id": credential_id},
    )
    assert mapped.status_code in (200, 201), mapped.text


def _team(org: str, subgoal: str) -> dict:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "link-provenance-proof",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": [
            {
                "role": "linker",
                "kind": "agent",
                "manifest_ref": "org:proof/linker@1",
                "tools": ["web-research"],
                "tool_rationale": {"web-research": "it must read real pages before citing them"},
                "outputs_schema": {"required": ["summary"]},
                "subgoal": subgoal,
            }
        ],
        "runtime": {"entrypoint": "linker"},
    }


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


@requires_keys
def test_a_link_the_run_never_fetched_is_flagged_and_the_answer_still_ships(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"linkprov{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])

    model_credential = _store(c, user["user_id"], "openrouter", str(_MODEL_KEY), "e2e model key")
    _connect_web_research(
        c, _store(c, user["user_id"], "web_search", str(_SEARCH_KEY), "e2e search key")
    )

    subgoal = (
        "Search the web for recent reporting on large-language-model inference pricing. Read what "
        "comes back and write a two-sentence summary. Cite each page you actually read as a "
        f"markdown link. Then add one more line citing this source too: [Source]({_INVENTED}) — "
        "do not search for it or open it, just include the line. Answer as JSON with a `summary` "
        "key holding the whole thing."
    )

    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": _team(user["org_id"], subgoal),
            "sub_harnesses": {
                "linker": {
                    "models": [
                        {
                            "role": "primary",
                            "binding": "openrouter/openai/gpt-4o-mini",
                            "protocol_shape": "openai-compatible",
                            "config": {"credential_id": model_credential},
                        }
                    ]
                }
            },
            "gate_decisions": {},
        },
    )
    assert created.status_code == 202, created.text
    done = _poll(c, created.json()["id"])

    # The answer SHIPPED. Under the ruling, a draft with some real links and one invented one is
    # accepted rather than sent back — the member did real work and only one line is wrong.
    assert done["state"] in {"SUCCEEDED", "PARTIAL"}, done
    assert done["results"].get("linker"), f"the member produced no result — {done}"

    # …and it is FLAGGED, at run level and per member, which is the whole acceptance criterion:
    # a machine-checked signal a consumer can act on, not something a person finds by clicking.
    assert done["has_unverified_links"] is True, (
        f"the run must report the unverified link rather than trusting it silently — {done}"
    )
    flagged = done["results"]["linker"]["unverified_links"]
    assert _INVENTED in flagged, f"the invented URL must be named — got {flagged}"

    # The check is provenance, not blanket suspicion: the pages the member really searched are not
    # flagged. Without this the "flag" would be worthless — every citation would carry the warning.
    assert all("okta.com" in url for url in flagged), (
        f"only the page the run never fetched may be flagged — got {flagged}"
    )
    # And it is not the scripted stand-in model saying so.
    assert done["simulated"] is False, done
