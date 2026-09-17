"""#1130 — a second run of the same seeded app must not lose its own artifacts to the first.

The Oraclous-provided Validation Desk app binds a deterministic sub-harness id per role
(``uuid.uuid5(app_id, role)``, ``execution-engine-service/.../domain/seed_apps/build.py:86``), so
every run of the SAME organisation's desk resolves to the SAME graph-ingest registry instance.
``harness_execution_service.py``'s deterministic-reuse branch (~1748-1765) inherits that instance's
stored configuration verbatim rather than rebinding it to the CURRENT run, so the second (and every
later) run's artifacts are filed under the FIRST run's ``team_run_id`` — the brief page's own query,
``GET /v1/artifacts?graph_id=...&team_run_id=<runId>`` (``apps/desk/src/pages/BriefPage.tsx:320-
327``), finds nothing for that run.

No fakes, no internal port, no DB-direct assertions (``FUCK_CLAUDE_FUCK_PAPERCLIP.md`` rule 5) —
this runs the SAME organisation's desk twice through the public gateway with a real model and reads
the artifact list exactly the way the brief page does. Follows
``tests/e2e/test_apps_platform_default_gateway_e2e.py``'s credential/connect/run helpers verbatim.

Needs OPENROUTER_API_KEY + TAVILY_API_KEY, same as that file's fully-connected test — neither is
spent by a prior run (the desk always searches fresh, the model call is metered but not a one-shot
token), so this is reproducible any time both keys are present; it does not depend on any
unreproducible or already-exhausted credential.

test-author does NOT run this against the deployed stack — the implementer proves it live (real
gateway, real model) before the ``[impl]`` PR merges, per CLAUDE.md §9.
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
_MODEL = os.environ["E2E_MODEL"]

#: The slug the engine seeds its first Oraclous-provided app under (same constant as
#: ``test_apps_platform_default_gateway_e2e.py``).
_DESK_SLUG = "validation-desk"

requires_byom = pytest.mark.skipif(
    _OR_KEY is None or _TAVILY_KEY is None,
    reason="OPENROUTER_API_KEY/TAVILY_API_KEY unset (the desk needs a model and a search tool)",
)


def _find(apps: list[dict], slug: str) -> dict | None:
    return next((a for a in apps if a.get("slug") == slug), None)


def _store_credential(
    client: httpx.Client, user_id: str, provider: str, key: str, name: str
) -> str:
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
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _connect_web_research(client: httpx.Client, *, credential_id: str) -> None:
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


def _run_desk(client: httpx.Client, desk_id: str, model_credential: str) -> str:
    started = client.post(
        f"/v1/engine/apps/{desk_id}/runs",
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
    return str(started.json()["id"])


def _wait_for_terminal(client: httpx.Client, run_id: str) -> str:
    deadline = time.time() + 900
    state = "QUEUED"
    while time.time() < deadline:
        state = client.get(f"/v1/engine/team-runs/{run_id}").json()["state"]
        if state in ("SUCCEEDED", "FAILED", "HALTED"):
            break
        time.sleep(10)
    return state


@requires_byom
@pytest.mark.byom  # a real model runs: the real-LLM leg, never the fake harness (#921)
def test_a_second_run_of_the_same_seeded_app_files_its_own_artifacts(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Run the desk twice for the SAME organisation and assert the SECOND run's own team_run_id
    finds its own artifacts — non-empty, and disjoint from the first run's.

    RED today (#1130): the second run reuses the first run's deterministic graph-ingest instance and
    inherits its stored producer/team_run_id verbatim, so the brief page's own query
    (``graph_id`` + the SECOND run's ``team_run_id``) comes back empty.
    """
    who = register("Repeat Desk Runner")
    client = gateway_client(who["token"])
    desk = _find(client.get("/v1/engine/apps").json()["apps"], _DESK_SLUG)
    assert desk is not None

    model_credential = _store_credential(
        client, who["user_id"], "openrouter", str(_OR_KEY), "e2e model key"
    )
    search_credential = _store_credential(
        client, who["user_id"], "web_search", str(_TAVILY_KEY), "e2e search key"
    )
    _connect_web_research(client, credential_id=search_credential)
    assert client.get(f"/v1/engine/apps/{desk['id']}/requirements").json()["ready"] is True

    run1_id = _run_desk(client, desk["id"], model_credential)
    assert _wait_for_terminal(client, run1_id) == "SUCCEEDED", (
        "the first run must succeed for the second run's comparison to mean anything"
    )

    run2_id = _run_desk(client, desk["id"], model_credential)
    assert _wait_for_terminal(client, run2_id) == "SUCCEEDED", (
        "the second run must succeed for its own artifacts to mean anything"
    )

    detail2 = client.get(f"/v1/engine/team-runs/{run2_id}").json()
    graph_id = detail2["graph_id"]
    assert graph_id, "the run carries no graph_id to look its artifacts up by"

    arts_run2 = client.get(f"/v1/artifacts?graph_id={graph_id}&team_run_id={run2_id}")
    assert arts_run2.status_code == 200, arts_run2.text
    listed_run2 = arts_run2.json()
    assert listed_run2, (
        f"the second run ({run2_id}) SUCCEEDED but the brief page's own query "
        f"(graph_id={graph_id}, team_run_id={run2_id}) found no artifacts — they are still filed "
        "under the first run (#1130)"
    )

    arts_run1 = client.get(f"/v1/artifacts?graph_id={graph_id}&team_run_id={run1_id}")
    assert arts_run1.status_code == 200, arts_run1.text
    run1_ids = {a["id"] for a in arts_run1.json()}
    run2_ids = {a["id"] for a in listed_run2}
    assert run1_ids.isdisjoint(run2_ids), (
        "the two runs' artifact lists share an id — the second run's write landed on the first "
        "run's artifact instead of filing its own"
    )
