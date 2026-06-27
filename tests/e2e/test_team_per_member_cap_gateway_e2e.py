"""#576 — a user-set per-member token cap is enforced end-to-end through the gateway (real BYOM).

The per-member runtime cap WAS the hardcoded policy tier (development-default = 200k), unraisable —
heavy team agents failed on Oraclous's own cap. This proves the USER's cap is what binds, on the
deployed stack through the gateway only: a member given a tiny ``max_tokens=100`` escalates the
token budget at the user's value (never the 200k tier) and produces NO draft, while the SAME member
with no cap runs and produces a full draft. The delta proves the cap threads
OHM → resolve_member_caps → HarnessClient → harness build_envelope → tool_use enforcement, and binds
at the user's value — not the hardcoded tier.

Real OpenRouter (BYOM through the gateway), nothing mocked (FUCK_CLAUDE_FUCK_PAPERCLIP rule 8). It
auto-skips when the key/gateway is absent (a skip is NOT a pass, rule 3).
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
    not _OR_KEY, reason="OPENROUTER_API_KEY unset (the real-model per-member-cap proof)"
)
_MODEL = "openrouter/openai/gpt-4o-mini"


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


def _write_writer_team() -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    (adir / "writer.md").write_text(
        "---\nname: writer\ntools: Read, Write\n---\n"
        "You draft a short, accurate paragraph on pour-over coffee brewing, then persist it with "
        "your Write tool so it is saved to the shared graph."
    )
    return root


def _import_writer(
    c: httpx.Client, user: dict, or_cred: str, *, max_tokens: int | None
) -> tuple[dict, dict]:
    from oraclous_ohm.import_.setup import import_setup

    imported = import_setup(
        _write_writer_team(),
        owner_organization_id=uuid.UUID(user["org_id"]),
        name="per-member-cap-writer",
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
    if max_tokens is not None:
        for member in doc["members"]:
            member["max_tokens"] = max_tokens  # #576: the user's per-member SAFETY CAP
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


def _writer_output(done: dict) -> str:
    results = done.get("results") or {}
    member = next(iter(results.values()), {}) or {}
    return str(member.get("output") or "")


@requires_byom
def test_tiny_per_member_cap_binds_at_the_users_value(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    # a user sets a TINY per-member cap (100 tokens). The first real-model turn already exceeds it,
    # so the member escalates the token budget at the USER's value — long before the 200k tier —
    # never persisting a draft — the unraisable-tier bug #576 fixes, proven from the far side.
    user = register(f"capmin{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "per-member-cap openrouter key")
    doc, subs = _import_writer(c, user, or_cred, max_tokens=100)

    done = _run(c, doc, subs, "per-member-cap-min")
    out = _writer_output(done)
    assert len(out) < 40, f"a 100-token-capped member must NOT produce a full draft — got {out!r}"


@requires_byom
def test_uncapped_member_runs_to_a_full_draft(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    # the control: the SAME member with NO per-member cap runs on the policy tier and produces a
    # real draft. The delta vs the tiny-cap run proves the escalation was the CAP, not the task.
    user = register(f"capnone{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "per-member-cap openrouter key")
    doc, subs = _import_writer(c, user, or_cred, max_tokens=None)

    done = _run(c, doc, subs, "per-member-cap-none")
    out = _writer_output(done)
    assert len(out) > 40, (
        f"an uncapped member must produce a full draft — got state={done.get('state')}"
    )
