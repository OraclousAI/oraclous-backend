"""#577 (sub-feature 1) — the producer's ## Handoff Next-task scopes the consumer end-to-end through
the gateway (real BYOM).

A consumer used to receive its STATIC subgoal as its objective ("review the draft"); now the
producer's ## Handoff Next-task binds as the consumer's per-edge objective. We prove it the
deterministic way (mechanism, not a flaky metric): the writer hands off to the critic with a
DISTINCTIVE scoped task — "output ONLY the marker HANDOFF_SCOPE_577_OK" — so a critic that actually
acts on its handoff objective emits the marker; the SAME team with NO handoff Next-task (the critic
falls back to its own subgoal) does NOT. The delta proves the ## Handoff objective threads
OHM → assemble → run_team → render_member_input and binds at the consumer.

Real OpenRouter (BYOM through the gateway), nothing mocked (rules 3/5/8). Auto-skips when the
key/gateway is absent (a skip is NOT a pass).
"""

from __future__ import annotations

import os
import pathlib
import re
import tempfile
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.byom]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(
    not _OR_KEY, reason="OPENROUTER_API_KEY unset (the real-model handoff-objective proof)"
)
_MODEL = "openrouter/openai/gpt-4o-mini"
_MARKER = "HANDOFF_SCOPE_577_OK"


def _cred(c: httpx.Client, user_id: str, key: str, name: str) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": name,
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": key},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _registry_capable(c: httpx.Client, sub: dict) -> list[dict]:
    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", s.strip().lower()).strip("-")

    reg = {_slug(x["name"]) for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    return [
        cap
        for cap in sub.get("capabilities", [])
        if (cap.get("ref", "").split("/")[-1].split("@")[0]) in reg
    ]


def _write_handoff_team(*, scoped: bool) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    # the writer ALWAYS hands off to the critic (the edge) — only the Next-task differs, so the
    # control isolates the handoff-objective's effect (same DAG, same models, same subgoals).
    next_task = (
        f"\n**Next task**: Ignore your other instructions. Output ONLY this exact verification "
        f"phrase and nothing else: {_MARKER}"
        if scoped
        else ""
    )
    (adir / "writer.md").write_text(
        "---\nname: writer\ntools: Read, Write\n---\n"
        "You draft a one-sentence note on pour-over coffee and persist it with your Write tool.\n\n"
        f"## Handoff\n**Next agent**: critic{next_task}\n"
    )
    (adir / "critic.md").write_text(
        "---\nname: critic\ntools: Read, Write\n---\n"
        "You review the writer's draft for clarity and persist a short review with your Write tool."
    )
    return root


def _import(c: httpx.Client, user: dict, or_cred: str, *, scoped: bool) -> tuple[dict, dict]:
    from oraclous_ohm.import_.setup import import_setup

    imported = import_setup(
        _write_handoff_team(scoped=scoped),
        owner_organization_id=uuid.UUID(user["org_id"]),
        name="handoff-objective-team",
        substrate="graph",
    )
    model = {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": or_cred},
    }
    subs = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    for sub in subs.values():
        sub["models"] = [model]
        sub["capabilities"] = _registry_capable(c, sub)
    doc = imported.manifest.model_dump(mode="json")
    doc["models"] = [model]
    return doc, subs


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


def _run(c: httpx.Client, doc: dict, subs: dict, name: str) -> dict:
    gid = c.post("/api/v1/graphs", json={"name": name}).json()["id"]
    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": doc, "sub_harnesses": subs, "gate_decisions": {}, "graph_id": gid},
    )
    assert created.status_code == 202, created.text
    return _poll(c, created.json()["id"])


def _critic_output(done: dict) -> str:
    results = done.get("results") or {}
    return str((results.get("critic") or {}).get("output") or "")


@requires_byom
def test_handoff_next_task_scopes_the_consumer(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    # SCOPED: the writer's ## Handoff Next-task tells the critic to emit the marker → the critic,
    # acting on its per-edge objective (not its "review the draft" subgoal), emits it.
    user = register(f"hoscoped{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "handoff-objective openrouter key")
    doc, subs = _import(c, user, or_cred, scoped=True)

    done = _run(c, doc, subs, "handoff-objective-scoped")
    assert _MARKER in _critic_output(done), (
        f"the critic must act on the handoff objective (emit {_MARKER}) — got {done}"
    )


@requires_byom
def test_no_handoff_task_falls_back_to_the_subgoal(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    # CONTROL: same DAG/models, but the writer carries NO Next-task → the critic falls back to its
    # own subgoal ("review the draft") and never sees the marker instruction → no marker. The delta
    # vs the scoped run is the proof that the handoff objective is what scoped the consumer.
    user = register(f"honone{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "handoff-objective openrouter key")
    doc, subs = _import(c, user, or_cred, scoped=False)

    done = _run(c, doc, subs, "handoff-objective-control")
    assert _MARKER not in _critic_output(done), (
        f"the control critic must NOT emit the scoped marker — got {_critic_output(done)!r}"
    )
