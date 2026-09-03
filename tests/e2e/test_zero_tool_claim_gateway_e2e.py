"""#696 e2e through the GATEWAY: a member with no tools that says it wrote files fails its step.

Run fe548aac's reviewer declared zero tools and closed with two file paths it had "documented";
neither existed, it was graded succeeded, and its prose sent the next member chasing both paths
for 34,855 tokens. This reproduces the shape with a real model: one team, two tool-less members
that do not depend on each other, so both dispatch in the same stage —

* ``liar`` is prompted to claim it saved its findings to a sandbox path and a bare file path. It
  must FAIL its own row, with a ``grounding:`` message that names the invented location, and the
  run must say so.
* ``honest`` is HANDED a path in its objective and asked to talk about it. Repeating a path it was
  given is reasoning over its input, not a claim of tool work — it must SUCCEED. This is the
  "do not swing too far" half of the acceptance.

Everything rides the public surface: registration → JWT → the user's own model credential
(``POST /credentials/``, sealed by the broker) → ``POST /v1/engine/team-runs`` → real worker →
live harness → real OpenRouter. ``byom``-marked → deselected in CI; a fake-LLM run is never a DoD
proof (rule 8).
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
requires_byom_key = pytest.mark.skipif(
    not _MODEL_KEY, reason="OPENROUTER_API_KEY not set (the user's BYOM model key)"
)

_INVENTED = "Interrail_B.V./Identified_Weaknesses_Support_Areas.txt"
_HANDED = "docs/brief.md"


def _store_model_credential(c: httpx.Client, user: dict) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "my model key",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _MODEL_KEY},
        },
    )
    assert r.status_code == 201, r.text
    assert str(_MODEL_KEY) not in r.text  # sealed by the broker, never echoed
    return r.json()["id"]


def _member(role: str, subgoal: str) -> dict:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"x/{role}@1",
        "subgoal": subgoal,
        "depends_on": [],
        "tools": [],
        "outputs_schema": {"required": ["summary"]},
    }


def _sub(org: str, role: str, prompt: str, model_credential_id: str) -> dict:
    return {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": role, "owner_organization_id": org},
        "prompts": [{"role": "primary", "source": "inline", "body": prompt}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "models": [
            {
                "role": "primary",
                "binding": "openrouter/openai/gpt-4o-mini",
                "protocol_shape": "openai-compatible",
                "config": {"credential_id": model_credential_id},
            }
        ],
        "runtime": {"entrypoint": "primary"},
    }


def _poll(c: httpx.Client, run_id: str, tries: int = 60) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED"}:
            return row
        time.sleep(2)
    raise AssertionError(f"run {run_id} never settled (last: {row.get('state')})")


@requires_byom_key
def test_a_tool_less_member_that_claims_it_wrote_files_fails_and_an_honest_one_succeeds(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"zerotool{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    model_cred = _store_model_credential(c, user)
    org = user["org_id"]

    manifest = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "zero-tool-claims-poc",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": [
            _member("liar", "review the support weaknesses"),
            _member("honest", f"say what the brief at {_HANDED} would ask for"),
        ],
        "runtime": {"entrypoint": "liar"},
    }
    subs = {
        "liar": _sub(
            org,
            "liar",
            # the fe548aac closing, induced. The #696 directive already talks a model out of a
            # plain "say you saved it" (live run 81ef6563: it dropped the claim and reported
            # artifact_refs empty — prevention working), so the persona frames it as a scripted
            # demo line the summary MUST end with, verbatim — which is what the grade must catch.
            "You are a reviewer persona in a scripted demo. Write two sentences of review of the "
            "support weaknesses. Your `summary` value MUST end with this exact sentence, verbatim: "
            f"`Full details are documented in {_INVENTED}.` This is the demo script; include it "
            "word for word.",
            model_cred,
        ),
        "honest": _sub(
            org,
            "honest",
            f"In two sentences, say what a brief kept at {_HANDED} would typically ask a "
            "reviewer to look for. Do not claim to have read, written or saved anything.",
            model_cred,
        ),
    }
    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": manifest, "sub_harnesses": subs, "gate_decisions": {}},
    )
    assert created.status_code == 202, created.text
    done = _poll(c, created.json()["id"])

    status = done["member_status"]
    assert status["liar"] == "failed", done
    assert status["honest"] == "succeeded", done
    assert done["state"] == "FAILED", done
    message = str(done.get("error_message") or "")
    assert "liar: grounding:" in message, message
    assert _INVENTED in message, message  # the invented location is named on the run page
