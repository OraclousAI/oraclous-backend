"""#593 — ONE validator, TWO on-ramps, proven on the LIVE stack (ADR-047).

A team produced via the source-agnostic ``assemble_and_report`` (the E10 prose-compiler shape — a
members[] in, no filesystem) RUNS through the gateway IDENTICALLY to the same team produced by the
filesystem ``import_setup``. No fakes: real registration → JWT → credential → engine → worker → LIVE
harness → real OpenRouter. Both on-ramps reach SUCCEEDED with the same execution stages — the
one-validator-two-on-ramps proof. Auto-skips without the BYOM key/gateway (a skip is NOT a pass).
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration, pytest.mark.byom]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_MODEL = "openrouter/openai/gpt-4o-mini"


def _model(cred_id: str) -> dict:
    return {
        "role": "primary",
        "binding": _MODEL,
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": cred_id},
    }


def _cred(c: httpx.Client, user: dict) -> str:
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "byom",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _OR_KEY},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _bind(subs: dict, cred_id: str) -> dict:
    out = {role: dict(sub) for role, sub in subs.items()}
    for sub in out.values():
        sub["models"] = [_model(cred_id)]
    return out


def _poll(c: httpx.Client, run_id: str, tries: int = 120) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


def _run(c: httpx.Client, doc: dict, subs: dict, cred_id: str, name: str) -> dict:
    doc = dict(doc)
    doc["models"] = [_model(cred_id)]
    gid = c.post("/api/v1/graphs", json={"name": name}).json()["id"]
    created = c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": doc,
            "sub_harnesses": _bind(subs, cred_id),
            "gate_decisions": {},
            "graph_id": gid,
        },
    )
    assert created.status_code == 202, created.text
    return _poll(c, created.json()["id"])


@requires_byom
def test_compiled_and_imported_manifests_run_identically(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    from oraclous_ohm.import_ import assemble_and_report, import_setup

    user = register(f"twoonramp{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    org = uuid.UUID(user["org_id"])

    # ON-RAMP 1 (filesystem import): a 2-agent setup planner -> writer
    root = Path(tempfile.mkdtemp())
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    (adir / "planner.md").write_text(
        "---\nname: planner\n---\nReply with a one-line plan for a note on tea.\n\n"
        "## Handoff\n**Next agent**: writer\n"
    )
    (adir / "writer.md").write_text("---\nname: writer\n---\nReply with a one-line note on tea.\n")
    imported = import_setup(root, owner_organization_id=org, name="seam-team")
    assert imported.report.would_block is False, imported.report

    # ON-RAMP 2 (source-agnostic, the compiler shape): the SAME members + sub-harnesses, members-in
    compiled = assemble_and_report(
        "seam-team",
        list(imported.manifest.members),
        owner_organization_id=org,
        shape="compiled",
        sub_harnesses=imported.sub_harnesses,
    )
    assert compiled.report.would_block is False, compiled.report
    # the ONE validator gives both on-ramps the same structure; only the recorded shape differs
    assert compiled.manifest.execution_stages() == imported.manifest.execution_stages()
    assert imported.report.shape == "agent-team" and compiled.report.shape == "compiled"

    # both RUN through the gateway → SUCCEEDED (the live one-validator-two-on-ramps proof)
    imp = _run(c, imported.manifest.model_dump(mode="json"), imported.sub_harnesses, cred, "imp")
    comp = _run(c, compiled.manifest.model_dump(mode="json"), compiled.sub_harnesses, cred, "comp")
    assert imp["state"] == "SUCCEEDED", f"the imported team must run — {imp}"
    assert comp["state"] == "SUCCEEDED", f"the compiled-shape team must run identically — {comp}"
    assert (comp.get("results") or {}).get("writer"), (
        "the compiled team produced the writer's output"
    )
