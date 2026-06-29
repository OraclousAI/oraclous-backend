"""#587 — a capped member that exhausts its budget DEGRADES to a flagged partial vs ESCALATES, by
``on_exhaustion``, end-to-end through the gateway (real BYOM).

Same team + same low per-member token cap, run twice flipping only ``budget.on_exhaustion``:
- ``degrade`` → the member's loop exhausts and FINISHES with its best-effort text — recorded
  ``member_status="partial"``, the team verdict is SUCCEEDED (a degrade is not a failure).
- ``escalate`` (default) → the member ESCALATES → the dispatch surfaces it as a member failure →
  the team is FAILED (today's behaviour, unchanged).

The contrast (SUCCEEDED/partial vs FAILED) on the SAME exhausting member is the proof that
``on_exhaustion`` chooses the behaviour. Real OpenRouter (BYOM through the gateway), nothing mocked
(rules 3/5/8). Auto-skips when the key/gateway is absent (a skip is NOT a pass).
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
    not _OR_KEY, reason="OPENROUTER_API_KEY unset (the real-model on_exhaustion proof)"
)
_MODEL = "openrouter/openai/gpt-4o-mini"
_CAP_TOKENS = 40  # a per-member token cap a single real response exceeds → the loop exhausts


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


def _write_solo_team() -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp())
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    (adir / "essayist.md").write_text(
        "---\nname: essayist\n---\n"
        "Write a detailed multi-paragraph essay on how photosynthesis works, step by step.\n"
    )
    return root


def _import_bind(
    c: httpx.Client, user: dict, or_cred: str, on_exhaustion: str
) -> tuple[dict, dict]:
    from oraclous_ohm.import_.setup import import_setup

    imported = import_setup(
        _write_solo_team(),
        owner_organization_id=uuid.UUID(user["org_id"]),
        name="on-exhaustion-solo",
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
    # the low per-member token cap + the on_exhaustion choice (#576 cap + #587 behaviour).
    doc["budget"] = {"max_tokens_per_member": _CAP_TOKENS, "on_exhaustion": on_exhaustion}
    return doc, subs


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED", "COST_BUDGET"}:
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


@requires_byom
def test_degrade_finishes_partial(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"degr{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "on-exhaustion openrouter key")
    doc, subs = _import_bind(c, user, or_cred, "degrade")

    done = _run(c, doc, subs, "on-exhaustion-degrade")
    # the exhausting member DEGRADED — a flagged partial, the team is NOT failed by it.
    assert done["state"] == "SUCCEEDED", f"a degrade completes the team (partial member) — {done}"
    assert done["member_status"].get("essayist") == "partial", f"member must degrade — {done}"
    assert (done.get("results") or {}).get("essayist"), "the best-effort partial output is surfaced"


@requires_byom
def test_escalate_fails_the_team(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"esca{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "on-exhaustion openrouter key")
    doc, subs = _import_bind(c, user, or_cred, "escalate")

    done = _run(c, doc, subs, "on-exhaustion-escalate")
    # the default escalate is unchanged — the exhausting member escalates → surfaced as a failure.
    assert done["state"] == "FAILED", (
        f"escalate (default) surfaces the budget gate as a fault — {done}"
    )
    assert done["member_status"].get("essayist") == "failed", f"member must escalate→fail — {done}"
