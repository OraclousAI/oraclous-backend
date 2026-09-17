"""#1109 — a team run whose only member's model key the provider refuses, END-TO-END through the
API GATEWAY (:8006), driven exactly as a real user would drive it.

A person pastes a key their OWN provider will actually reject (never a fake key against a fake
harness — the harness must be in LIVE mode, or nothing calls the provider and a bad key is never
distinguished from a good one, per #1108). The run reaches FAILED and the per-member curated token
``member_error_codes[<role>] == "llm_credential_rejected"`` names WHICH member failed and WHY — a
shape the console can act on, distinct from the run's free-text ``error_message`` (which the
gateway's error-body drain would strip anyway). Neither the rejected key nor its credential id ever
reach the caller.

Mirrors ``test_intake_readback_gateway_e2e.py``'s
``test_a_key_the_provider_refuses_is_named_as_such`` (#1108): same bogus
``sk-or-v1-...`` key stored through the real credential API, same leak-safety assertions, same
requirement that the harness actually be LIVE (gated on ``OPENROUTER_API_KEY`` being set — the byom
marker — even though the key used here is deliberately wrong).
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(
    _OR_KEY is None, reason="OPENROUTER_API_KEY unset (needed to run the harness LIVE)"
)
_MODEL = os.environ["E2E_MODEL"]

_TASK_KEY = "task"


def _model(credential_id: str) -> dict:
    return {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
    }


def _bogus_credential(c: httpx.Client, user: dict) -> tuple[str, str]:
    """Store a key the provider will actually reject, through the real credential API — exactly
    like a mistyped or revoked key a real user might paste in (#1108)."""
    bogus_key = "sk-or-v1-" + uuid.uuid4().hex + uuid.uuid4().hex
    cred = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "refused model key",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": bogus_key},
        },
    )
    assert cred.status_code == 201, cred.text
    return str(cred.json()["id"]), bogus_key


def _one_member_team(user: dict, model: dict) -> tuple[dict, dict]:
    """A minimal, real one-member reasoning-only team declaring ``task_input`` — the smallest shape
    that reaches the harness's own LLM client and can therefore be refused by the provider."""
    from oraclous_ohm.import_.setup import import_setup

    root = pathlib.Path(tempfile.mkdtemp())
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "writer.md").write_text(
        "---\nname: writer\n---\nAnswer the task you are handed in one short sentence.\n"
    )
    imported = import_setup(
        root, owner_organization_id=uuid.UUID(user["org_id"]), name="refused-key-team-e2e"
    )
    assert imported.manifest is not None
    subs = {role: {**sub, "models": [model]} for role, sub in imported.sub_harnesses.items()}
    doc = imported.manifest.model_dump(mode="json")
    doc["models"] = [model]
    doc["task_input"] = {"required": True, "key": _TASK_KEY, "description": "The task."}
    return doc, subs


def _poll(c: httpx.Client, run_id: str, tries: int = 160) -> httpx.Response:
    resp: httpx.Response | None = None
    for _ in range(tries):
        resp = c.get(f"/v1/engine/team-runs/{run_id}")
        if resp.json()["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return resp
        time.sleep(3)
    last = resp.json() if resp is not None else None
    raise AssertionError(f"run {run_id} never terminated (last: {last})")


@requires_byom
def test_a_refused_model_key_fails_the_run_and_names_the_member(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"memberrefused{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    credential_id, bogus_key = _bogus_credential(c, user)
    model = _model(credential_id)
    doc, subs = _one_member_team(user, model)
    role = next(m["role"] for m in doc["members"])

    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": doc,
            "sub_harnesses": subs,
            "gate_decisions": {},
            "inputs": {_TASK_KEY: "Say hello in one short sentence."},
        },
    )
    assert created.status_code == 202, created.text
    resp = _poll(c, str(created.json()["id"]))
    row = resp.json()

    assert row["state"] == "FAILED", row
    assert row["member_error_codes"].get(role) == "llm_credential_rejected", row

    # leak-safety (#1108 precedent): neither the refused key nor its credential id ever reach the
    # caller, in ANY field of the read — not just the curated code.
    assert bogus_key not in resp.text, resp.text
    assert credential_id not in resp.text, resp.text
