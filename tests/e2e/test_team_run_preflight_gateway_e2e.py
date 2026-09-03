"""#664 e2e through the GATEWAY: GO on a team whose tool is unconfigured never starts a run.

The GO sheet promises "Tool credentials are checked at run time — a missing one stops the run with
a connect prompt". On run ``c904fade`` the ``github-reader`` instance was CONFIGURATION_REQUIRED
and the run started anyway: six 409s and 5,800 tokens before it failed. This drives the promised
check as the user would meet it: the user creates a ``github-reader`` instance and does NOT bind a
credential (the registry marks it CONFIGURATION_REQUIRED), then presses GO on a team whose member
declares that capability. The answer must be the connect prompt — CREDENTIALS_REQUIRED naming the
capability and the credential type — and NO run may exist afterwards.

Keyless on purpose: the refusal happens before any model is needed, so this runs in the
deterministic suite. The admitted side (a READY instance → 202 → the run reuses it and succeeds)
is ``test_org_instance_reuse_gateway_e2e.py`` (#663), which now also proves the pre-flight admits
a configured instance.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _unconfigured_reader_instance(c: httpx.Client) -> str:
    caps = {x["name"]: x for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    assert "GitHub Reader" in caps, "the curated github-reader is not seeded"
    inst = c.post(
        "/api/v1/instances",
        json={"capability_id": caps["GitHub Reader"]["id"], "name": "github-reader"},
    )
    assert inst.status_code in (200, 201), inst.text
    assert inst.json().get("status") == "CONFIGURATION_REQUIRED", inst.text
    return inst.json()["id"]


def _team(org: str) -> tuple[dict, dict]:
    """A one-member team whose fetcher DECLARES ``core/github-reader`` — the compiled-team shape
    (member ``tools[]`` = ceiling, sub-harness ``capabilities[]`` = grant, #659). No model
    binding: the refusal must land before a model is ever looked at."""
    manifest = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "preflight-poc",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": [
            {
                "role": "fetcher",
                "kind": "agent",
                "manifest_ref": "x/fetcher@1",
                "subgoal": "list the repository's files",
                "depends_on": [],
                "tools": ["github-reader"],
                "outputs_schema": {"required": ["summary"]},
            }
        ],
        "runtime": {"entrypoint": "fetcher"},
    }
    sub = {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": "fetcher", "owner_organization_id": org},
        "prompts": [{"role": "primary", "source": "inline", "body": "list the files"}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "capabilities": [{"ref": "core/github-reader@1.0.0", "binding": "github-reader"}],
        "runtime": {"entrypoint": "primary"},
    }
    return manifest, sub


def _run_total(c: httpx.Client) -> int:
    resp = c.get("/v1/engine/team-runs")
    assert resp.status_code == 200, resp.text
    return int(resp.json()["total"])


def test_go_on_an_unconfigured_tool_is_a_connect_prompt_and_no_run_exists(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"preflight{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])

    _unconfigured_reader_instance(c)
    runs_before = _run_total(c)
    manifest, sub = _team(user["org_id"])

    resp = c.post(
        "/v1/engine/team-runs",
        json={"manifest": manifest, "sub_harnesses": {"fetcher": sub}, "gate_decisions": {}},
    )

    # the promised connect prompt, in the shape the console already renders (#483 / FE #186)
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "CREDENTIALS_REQUIRED", err
    assert err["retryable"] is False, err
    assert err["needs_credential"] == {"requirement_id": "api_key", "provider": "github-reader"}
    # and nothing was spent: no run was created for this org
    assert _run_total(c) == runs_before


def test_go_on_a_team_with_no_tools_is_not_affected_by_the_preflight(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """Criterion 4, at the gateway: a pure-reasoning team's GO is still a 202. The run itself is
    not driven to completion here (that is the baseline suite's job); this pins that the
    pre-flight did not turn a tool-less team away."""
    user = register(f"preflightnt{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    manifest = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "no-tools-poc",
            "owner_organization_id": user["org_id"],
            "kind": "team",
        },
        "members": [
            {
                "role": "thinker",
                "kind": "agent",
                "manifest_ref": "x/thinker@1",
                "subgoal": "think",
                "depends_on": [],
                "tools": [],
                "outputs_schema": {"required": ["summary"]},
            }
        ],
        "runtime": {"entrypoint": "thinker"},
    }
    sub = {
        "ohm_version": "1.0",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "thinker",
            "owner_organization_id": user["org_id"],
        },
        "prompts": [{"role": "primary", "source": "inline", "body": "think briefly"}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }
    resp = c.post(
        "/v1/engine/team-runs",
        json={"manifest": manifest, "sub_harnesses": {"thinker": sub}, "gate_decisions": {}},
    )
    assert resp.status_code == 202, resp.text
