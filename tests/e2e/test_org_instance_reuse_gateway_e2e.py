"""#663 e2e through the GATEWAY: a team run uses the org's CONFIGURED github-reader instance.

The UC-D7 failure, reproduced as a user journey: the user connects a GitHub credential, configures
a ``github-reader`` instance (READY), then runs a team whose member declares that capability. Pre-
fix, the run minted its own per-harness instance — never bound to the credential — and every
dispatch 409'd (``CONFIGURATION_REQUIRED``); the member then failed the #642 grounding gate and the
run FAILED after burning real tokens. Post-fix, the run binds the org's configured instance, the
member's ``list_files`` call succeeds against the real GitHub API, and the run SUCCEEDS with
grounded receipts — and the org's instance list gains NO new unconfigured copy of the capability.

Everything rides the public surface: real registration → JWT → credential API (PAT sealed by the
broker, never injected) → instance API → ``POST /v1/engine/team-runs`` → real worker → live
harness → real OpenRouter + real GitHub. Requires (local run, ``scripts/e2e.sh``):

  - ``HARNESS_LLM_MODE=live`` + ``OPENROUTER_API_KEY``   (the member must really call the tool)
  - ``GITHUB_DELIVER_PAT`` + ``GITHUB_DELIVER_REPO``     (a real repo the PAT can read)

``github``+``byom``-marked → deselected in CI; a fake-LLM run is never a DoD proof (rule 8).
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom, pytest.mark.github]

_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")
_PAT = os.environ.get("GITHUB_DELIVER_PAT")
_REPO = os.environ.get("GITHUB_DELIVER_REPO")  # owner/repo the PAT can read
requires_keys = pytest.mark.skipif(
    not (_MODEL_KEY and _PAT and _REPO),
    reason="OPENROUTER_API_KEY/GITHUB_DELIVER_PAT/GITHUB_DELIVER_REPO unset (#663 keyed e2e)",
)


def _store_credential(c: httpx.Client, user: dict, provider: str, secret: str, name: str) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": name,
            "provider": provider,
            "cred_type": "api_key",
            "credential": {"api_key": secret},
        },
    )
    assert r.status_code == 201, r.text
    assert secret not in r.text  # sealed by the broker, never echoed
    return r.json()["id"]


def _configured_reader_instance(c: httpx.Client, cred_id: str) -> str:
    """The user's OWN ``github-reader`` instance, credential-bound → READY — the instance the
    team run must reuse (#663 acceptance 1)."""
    caps = {x["name"]: x for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    inst = c.post(
        "/api/v1/instances",
        json={
            "capability_id": caps["GitHub Reader"]["id"],
            "name": "github-reader",
            "configuration": {},
        },
    )
    assert inst.status_code in (200, 201), inst.text
    instance_id = inst.json()["id"]
    bind = c.post(
        f"/api/v1/instances/{instance_id}/configure-credentials",
        json={"credential_mappings": {"api_key": cred_id}},
    )
    assert bind.status_code in (200, 201), bind.text
    assert bind.json().get("status") == "READY", bind.text
    return instance_id


def _team(org: str, model_credential_id: str) -> tuple[dict, dict]:
    """A one-member team whose fetcher DECLARES ``core/github-reader`` (the compiled-team shape:
    member ``tools[]`` = ceiling, sub-harness ``capabilities[]`` = grant, #659)."""
    manifest = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "reader-reuse-poc",
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
                "tools": ["core/github-reader@1.0.0"],
            }
        ],
        "runtime": {"entrypoint": "fetcher"},
    }
    sub = {
        "ohm_version": "1.0",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "fetcher",
            "owner_organization_id": org,
        },
        "prompts": [
            {
                "role": "primary",
                "source": "inline",
                "body": (
                    f"Call github-reader__list_files with repo='{_REPO}' and path='' and report "
                    "the file names you actually received."
                ),
            }
        ],
        "actors": [{"role": "primary", "kind": "agent"}],
        "models": [
            {
                "role": "primary",
                "binding": "openrouter/openai/gpt-4o-mini",
                "protocol_shape": "openai-compatible",
                "config": {"credential_id": model_credential_id},
            }
        ],
        "capabilities": [{"ref": "core/github-reader@1.0.0", "binding": "github-reader"}],
        "runtime": {"entrypoint": "primary"},
    }
    return manifest, sub


def _poll(c: httpx.Client, run_id: str, tries: int = 45) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED"}:
            return row
        time.sleep(2)
    raise AssertionError(f"run {run_id} never settled (last: {row.get('state')})")


@requires_keys
def test_a_team_run_reuses_the_orgs_configured_reader_and_succeeds(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register("Instance Reuse User")
    c = gateway_client(user["token"])

    # the user's own credentials, through the public API — model key + GitHub PAT, both sealed
    model_cred = _store_credential(c, user, "openrouter", str(_MODEL_KEY), "my model key")
    gh_cred = _store_credential(c, user, "github", str(_PAT), "my github pat")
    reader_instance = _configured_reader_instance(c, gh_cred)

    before = {i["id"] for i in c.get("/api/v1/instances").json()["instances"]}

    manifest, sub = _team(user["org_id"], model_cred)
    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": manifest, "sub_harnesses": {"fetcher": sub}, "gate_decisions": {}},
    )
    assert created.status_code == 202, created.text
    done = _poll(c, created.json()["id"])

    # pre-fix this run FAILED: the minted instance 409'd and the grounding gate (#642) failed the
    # member. Post-fix the org's configured instance answers and the member's receipts are real.
    assert done["state"] == "SUCCEEDED", done

    # acceptance 1: reuse means NO new unconfigured copy of the capability appeared for this org
    after = c.get("/api/v1/instances").json()["instances"]
    minted = [
        i for i in after if i["id"] not in before and i.get("status") == "CONFIGURATION_REQUIRED"
    ]
    assert minted == [], f"the run minted unconfigured instances: {minted}"
    assert reader_instance in {i["id"] for i in after}  # the user's instance is still theirs
