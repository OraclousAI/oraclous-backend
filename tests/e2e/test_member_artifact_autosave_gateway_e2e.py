"""#1137 acceptance e2e, through the GATEWAY: a member with NO tools still gets its declared
deliverable saved.

#1142 note: this test's member is asked for exactly ``posture``/``headline`` and nothing else, so
its live answer is not expected to carry richer content — the assertion below is loosened from an
exact-set match to a superset check (declared keys present, envelope keys absent) so it keeps
proving #1137's contract without depending on whether a cheap model happens to add stray keys of
its own. The richer-content behaviour itself (#1142) is pinned at the domain layer in
``test_member_artifact_document.py``; a live case for a member whose answer genuinely carries
undeclared keys is left to the implementer/qa-engineer as a fast-follow, the same way #1141 already
tracks the tool-calling live case #1137 owed.

Validation Desk's decision brief was lost on 5 of 5 runs because nothing enforced the model
actually calling its save tool. This is the sharpest possible proof that the FIX does not depend
on the model calling anything: the member declares NO tools at all — not even a bound ``Write`` —
so it is physically incapable of ingesting its own answer. If a document still lands under this
run and role, it can only be the PLATFORM's own settle-time write (#1137's ``should_autosave`` /
``build_document`` / ``ArtifactsClient.ingest``), never the model's.

A real user, through the application-gateway on ``:8006`` only — real registration, the user's own
BYOM model credential, the public team-run and artifacts endpoints. No service port, no
``/internal``, no DB-direct assertion, nothing mocked or monkeypatched. No web search is used
anywhere in this test (the account's Tavily allowance is spent, #1104), and the member declares no
capabilities at all, so the search-key requirement never applies here.

The ``byom``-marked proof leg needs a real BYOM key and auto-skips without one. A skip is NOT a
pass (rule 3): run it locally with ``deploy/.env``'s ``OPENROUTER_API_KEY`` and a LIVE harness
(``scripts/e2e.sh --byom``).
"""

from __future__ import annotations

import json
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
_MODEL = os.environ["E2E_MODEL"]

_ROLE = "analyst"


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


def _new_graph(c: httpx.Client, name: str) -> str:
    g = c.post("/api/v1/graphs", json={"name": f"{name}-{uuid.uuid4().hex[:6]}"})
    assert g.status_code == 201, g.text
    return str(g.json()["id"])


def _manifest(org: str) -> dict:
    """One member, NO tools, a declared deliverable — the exact trigger shape #1137 rules on."""
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "member-autosave-poc",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": [
            {
                "role": _ROLE,
                "kind": "agent",
                "manifest_ref": f"x/{_ROLE}@1",
                "subgoal": "judge a fictional launch's readiness from the brief alone",
                "depends_on": [],
                "tools": [],  # NO tools at all — it cannot call a save/ingest tool even if it tried
                "outputs_schema": {"required": ["posture", "headline"]},
            }
        ],
        "runtime": {"entrypoint": _ROLE},
    }


def _sub(org: str, prompt: str, model_credential_id: str) -> dict:
    return {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": _ROLE, "owner_organization_id": org},
        "prompts": [{"role": "primary", "source": "inline", "body": prompt}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "models": [
            {
                "role": "primary",
                "binding": _MODEL,
                "protocol_shape": "openai-compatible",
                "config": {"credential_id": model_credential_id},
            }
        ],
        "runtime": {"entrypoint": "primary"},
    }


def _poll(c: httpx.Client, run_id: str, tries: int = 90) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(
        f"run {run_id} never reached a terminal state within {tries * 3}s "
        f"(last observed state: {row.get('state')!r})"
    )


def _await_document(
    c: httpx.Client, graph_id: str, run_id: str, role: str, tries: int = 30
) -> dict:
    """Retry the read until the platform's best-effort, settle-time save has landed. The write
    happens off the request that flips the run terminal, so a first empty read is expected, not a
    failure."""
    for _ in range(tries):
        r = c.get(
            "/v1/artifacts",
            params={"graph_id": graph_id, "team_run_id": run_id, "member_role": role},
        )
        assert r.status_code == 200, r.text
        rows = r.json()
        if rows:
            return rows[0]
        time.sleep(2)
    raise AssertionError(f"no document landed for run {run_id} / member {role} within {tries * 2}s")


@requires_byom_key
def test_a_tool_less_members_declared_deliverable_is_saved_by_the_platform(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
    assert_run_succeeded: Callable[..., None],
) -> None:
    user = register(f"autosave{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    org = user["org_id"]
    model_cred = _store_model_credential(c, user)
    nonce = uuid.uuid4().hex[:10]

    graph_id = _new_graph(c, "member-autosave")
    manifest = _manifest(org)
    subs = {
        _ROLE: _sub(
            org,
            "You have NO tools available in this run — do not attempt to call any, you have "
            "none. A fictional product launch has three features done and two not done. Judge, "
            "from that alone, whether it is ready to ship. Answer with your `posture` (one of: "
            "ready, not ready, needs more time) and a one-sentence `headline` summarising your "
            f"judgement. Your `headline` MUST include this exact token, verbatim: {nonce}.",
            model_cred,
        )
    }
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": manifest,
            "sub_harnesses": subs,
            "gate_decisions": {},
            "graph_id": graph_id,
        },
    )
    assert created.status_code == 202, created.text
    run_id = str(created.json()["id"])

    # ADR-042: a cheap model occasionally answers in prose without its required keys — a property
    # of the model, not of the behaviour under test — so retry it rather than flake on that.
    done = _poll(c, run_id)
    for _ in range(3):
        if done["state"] == "SUCCEEDED":
            break
        assert done["state"] == "FAILED", done
        rerun = c.post(f"/v1/engine/team-runs/{run_id}/rerun")
        assert rerun.status_code == 202, rerun.text
        done = _poll(c, run_id)
    assert_run_succeeded(done, state_key="state")
    # RULE 8: only a real LLM echoes the per-run nonce — a fake-mode run cannot.
    assert nonce in str(done["results"]), (
        f"nonce {nonce!r} in no result — was the harness LIVE? results={done['results']!r}"
    )
    member_payload = done["results"][_ROLE]
    assert member_payload.get("posture"), member_payload
    assert nonce in str(member_payload.get("headline")), member_payload

    # THE POINT: the member declared no tools at all, so it could not have written this itself —
    # a document under this run and role can only be the platform's own settle-time save.
    row = _await_document(c, graph_id, run_id, _ROLE)
    assert row["producer_kind"] == "team-member", row
    assert row["team_run_id"] == run_id, row
    assert row["member_role"] == _ROLE, row

    detail = c.get(f"/v1/artifacts/{row['id']}")
    assert detail.status_code == 200, detail.text
    content = json.loads(detail.json()["content"] or "{}")
    assert content.get("posture") == member_payload["posture"], content
    assert content.get("headline") == member_payload["headline"], content
    # #1142: the declared keys must always be present — a superset is fine now (the member's whole
    # final answer, if it parsed one), but the run's bookkeeping must never be part of it.
    assert {"posture", "headline"}.issubset(content), content
    for envelope_key in ("status", "simulated", "unverified_links", "fetched_urls", "output"):
        assert envelope_key not in content, (envelope_key, content)
