"""#578 (ADR-046) — the human-gate REVISE (reject → revision → re-invoke) loop, END-TO-END through
the application gateway with real BYOM.

Surfaced by the #440 book-GO e2e: rejecting a gate terminated the run with no path to revise.
ADR-046 wires a THIRD gate verb — ``revise`` — that re-runs the rejected producer's invalidated
sub-tree with the human's feedback threaded in, re-pauses at the same gate, and is bounded by
``max_revisions`` (fail-closed → terminal REJECTED). This drives the REAL book-studio team through
the deployed stack (gateway → engine → worker → live harness → real OpenRouter) and proves:

  1. the studio book charter (its ``kind:human`` gate chain) PAUSES at GATE A;
  2. ``revise`` re-runs GATE A's producer sub-tree + RE-PAUSES at the same gate (revision_rounds→1);
  3. ``approve`` then CROSSES the revised gate (the run advances past GATE A);
  4. exhausting ``max_revisions`` fail-closes the run to terminal REJECTED.

Real OpenRouter (BYOM through the gateway), nothing mocked / no DB-direct / no service-port shortcut
(FUCK_CLAUDE rules 3/5/8). Auto-skips when the key/gateway is absent (a skip is NOT a pass).
"""

from __future__ import annotations

import os
import pathlib
import re
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.byom]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(
    not _OR_KEY, reason="OPENROUTER_API_KEY unset (the real-model human-gate revise proof)"
)
_MODEL = "openrouter/openai/gpt-4o-mini"
_FIX = pathlib.Path(__file__).resolve().parents[2] / "packages/ohm/tests/fixtures/book-team"
_BOOK_STUDIO = _FIX / ".claude" / "skills" / "book-studio"


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


def _bind(c: httpx.Client, imported: object, or_cred: str) -> tuple[dict, dict]:
    """Bind the BYOM model onto the imported manifest + each sub-harness (the import→run seam)."""
    model = {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": or_cred},
    }
    subs = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}  # type: ignore[attr-defined]
    for sub in subs.values():
        sub["models"] = [model]
        sub["capabilities"] = _registry_capable(c, sub)
    doc = imported.manifest.model_dump(mode="json")  # type: ignore[attr-defined]
    doc["models"] = [model]
    return doc, subs


def _poll(c: httpx.Client, run_id: str, tries: int = 160) -> dict:
    """Poll until the run settles at a PAUSE or a terminal state (QUEUED/RUNNING keep polling, so a
    resume's QUEUED→RUNNING→PAUSED re-pause is awaited, not caught mid-flight)."""
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never settled (last: {row.get('state')})")


def _advance(c: httpx.Client, run_id: str, decisions: dict) -> None:
    r = c.post(f"/v1/engine/team-runs/{run_id}/advance", json={"gate_decisions": decisions})
    assert r.status_code == 202, r.text


def _start_book_run(
    c: httpx.Client, user: dict, or_cred: str, *, max_revisions: int | None = None
) -> dict:
    """Import the REAL book-studio team, bind BYOM, run it through the gateway, and return the row
    PAUSED at GATE A (the studio's first ``kind:human`` author gate)."""
    from oraclous_ohm.import_.setup import import_setup

    imported = import_setup(
        _BOOK_STUDIO, owner_organization_id=uuid.UUID(user["org_id"]), name="book-revise"
    )
    assert next(m for m in imported.manifest.members if m.role == "gate-a").kind == "human"
    doc, subs = _bind(c, imported, or_cred)
    if max_revisions is not None:  # merge ONLY max_revisions — preserve the studio's orchestration
        orch = doc.get("orchestration") or {}
        term = orch.get("termination") or {}
        doc["orchestration"] = {**orch, "termination": {**term, "max_revisions": max_revisions}}
    gid = c.post("/api/v1/graphs", json={"name": "book-revise"}).json()["id"]
    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": doc, "sub_harnesses": subs, "gate_decisions": {}, "graph_id": gid},
    )
    assert created.status_code == 202, created.text
    row = _poll(c, created.json()["id"])
    assert row["state"] == "PAUSED" and "gate-a" in row["paused_at"], row
    return row


@requires_byom
def test_revise_reruns_gate_a_producer_subtree_repauses_then_approve_crosses(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"bookrev{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "revise openrouter key")

    paused = _start_book_run(c, user, or_cred)
    run_id = paused["id"]
    assert paused["revision_rounds"] == {}  # not yet revised
    assert "chapter-architect" not in (paused.get("results") or {})  # halted AT the gate

    # (2) REVISE gate-a — the author sends the pre-gate producer sub-tree back with feedback.
    _advance(
        c,
        run_id,
        {
            "gate-a": {
                "decision": "revise",
                "feedback": "tighten the calibration to the bible voice",
            }
        },
    )
    revised = _poll(c, run_id)
    # the deployed engine re-ran the producer sub-tree and RE-PAUSED at the SAME gate — not crossed.
    assert revised["state"] == "PAUSED", revised
    assert "gate-a" in revised["paused_at"], revised
    assert revised["revision_rounds"] == {"gate-a": 1}  # the revise round was recorded, bounded
    assert "chapter-architect" not in (revised.get("results") or {})  # still behind the gate

    # (3) APPROVE gate-a — the loop resolves and the run CROSSES the (now revised) gate.
    _advance(c, run_id, {"gate-a": "approve"})
    crossed = _poll(c, run_id)
    assert crossed["state"] in {"PAUSED", "SUCCEEDED"}, crossed
    assert "gate-a" not in crossed["paused_at"], crossed  # advanced PAST the revised gate


@requires_byom
def test_revise_beyond_max_revisions_fail_closes_to_rejected(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"bookrej{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "reject openrouter key")

    # tighten the bound to 1 so the loop fail-closes after a single revision (fast + deterministic).
    paused = _start_book_run(c, user, or_cred, max_revisions=1)
    run_id = paused["id"]

    _advance(c, run_id, {"gate-a": {"decision": "revise", "feedback": "first pass"}})
    first = _poll(c, run_id)
    assert first["state"] == "PAUSED" and first["revision_rounds"] == {"gate-a": 1}, first

    # the 2nd revise exceeds max_revisions=1 → the run fail-closes to terminal REJECTED (no re-
    # drive). A rejected run is done — there is no path past a definitively-rejected gate.
    _advance(c, run_id, {"gate-a": {"decision": "revise", "feedback": "second pass"}})
    rejected = _poll(c, run_id)
    assert rejected["state"] == "REJECTED", rejected
    assert "revision limit" in (rejected.get("error_message") or ""), rejected
