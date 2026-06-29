"""#577 slice-2 + slice-3 — the book-team imports + runs end-to-end through the gateway (real BYOM).

slice-2 (prose-coordinator): the REAL book-studio coordinator (a numbered ``chapter`` pipeline, no
``modules/`` layout) imports via ``import_setup`` (the PRODUCTION entry, not a direct adapter call)
into the team DAG and RUNS through the gateway: research-scout → bible-keeper → book-calibrate then
PAUSES at ``gate-a`` (a ``kind:human`` author gate). The pause through the deployed engine is the
load-bearing M1 behaviour — the pipeline halts for the author, it does not run past the gate.

slice-3 (skill-driver): a REAL agent referencing the reader-panel uv-CLI skill imports with the CLI
staged on its sub-harness ``runtime.driver`` (command/entry/env), and that driver-bearing manifest
flows through the import→gateway seam and RUNS — the engine accepts the staged driver (back-compat);
the venv dispatch is the harness-runtime's deferred job, out of this importer slice.

Real OpenRouter (BYOM through the gateway), nothing mocked (rules 3/5/8). Auto-skips when the
key/gateway is absent (a skip is NOT a pass).
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.byom]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(
    not _OR_KEY, reason="OPENROUTER_API_KEY unset (the real-model book-chapter proof)"
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
    """Bind the BYOM model onto the imported manifest + each sub-harness (the import->run seam)."""
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


def _poll(c: httpx.Client, run_id: str, tries: int = 140) -> dict:
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


def _write_driver_team(base: pathlib.Path) -> pathlib.Path:
    """A real agent-team referencing the reader-panel uv-CLI skill (slice-3), built from the REAL
    fixture skill + uv package so the driver detection runs against the genuine pyproject. Built
    under ``base`` (the test's tmp_path) so pytest cleans it up — no leaked temp dirs."""
    root = base / "driver-team"
    shutil.copytree(
        _FIX / ".claude" / "skills" / "reader-panel", root / ".claude/skills/reader-panel"
    )
    shutil.copytree(_FIX / "reader-panel", root / "reader-panel")  # the team-root sibling package
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    # reasoning-only (no tools) — the driver is staged from the SKILL, not from tools, and a
    # tool-less agent converges in one turn (a tool-bearing one can loop). The point is the
    # driver-bearing manifest RUNS through the deployed seam, not that this agent uses a tool.
    (adir / "panel-runner.md").write_text(
        "---\nname: panel-runner\nskills: reader-panel\n---\n"
        "You output ONLY the single word READY and nothing else.\n"
    )
    return root


@requires_byom
def test_book_chapter_pipeline_pauses_at_the_author_gate(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    from oraclous_ohm.import_.setup import import_setup

    user = register(f"bookchap{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "book-chapter openrouter key")

    # slice-2: the REAL book-studio prose coordinator imports through the PRODUCTION entry.
    imported = import_setup(
        _BOOK_STUDIO, owner_organization_id=uuid.UUID(user["org_id"]), name="book-chapter"
    )
    roles = {m.role for m in imported.manifest.members}
    assert {
        "research-scout",
        "gate-a",
        "gate-b",
        "gate-c",
    } <= roles  # the chapter DAG + author gates
    assert next(m for m in imported.manifest.members if m.role == "gate-a").kind == "human"

    doc, subs = _bind(c, imported, or_cred)
    done = _run(c, doc, subs, "book-chapter-pipeline")

    # the deployed engine ran the pre-gate agents and HALTED at the first author gate — not past it.
    assert done["state"] == "PAUSED", f"the chapter pipeline must pause at the author gate — {done}"
    assert "gate-a" in done["paused_at"], f"must pause at gate-a, paused_at={done.get('paused_at')}"
    results = done.get("results") or {}
    assert "book-calibrate" in results  # the pre-gate stage ran
    assert (
        "chapter-architect" not in results
    )  # but the run HALTED at the gate — no post-gate member


@requires_byom
def test_skill_driver_manifest_imports_and_runs_through_the_gateway(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
    tmp_path: pathlib.Path,
) -> None:
    from oraclous_ohm.import_.setup import import_setup

    user = register(f"bookdrv{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    or_cred = _cred(c, user["user_id"], str(_OR_KEY), "book-driver openrouter key")

    # slice-3: the agent referencing the reader-panel uv-CLI skill imports with the CLI staged.
    imported = import_setup(
        _write_driver_team(tmp_path),
        owner_organization_id=uuid.UUID(user["org_id"]),
        name="book-driver",
    )
    driver = imported.sub_harnesses["panel-runner"]["runtime"]["driver"]
    assert driver is not None  # the staged CLI contract rode through import_setup → the run seam
    assert driver["command_name"] == "reader-panel"
    assert driver["entry_point"] == "reader_panel.cli:main"
    assert "ANTHROPIC_API_KEY" in driver["env"]

    # the driver-bearing manifest flows through the deployed seam and RUNS (back-compat): the engine
    # accepts the staged driver and runs the agent; the venv dispatch is the harness-runtime's job.
    doc, subs = _bind(c, imported, or_cred)
    done = _run(c, doc, subs, "book-driver-run")
    assert done["state"] == "SUCCEEDED", f"the driver-bearing manifest must run, not break — {done}"
