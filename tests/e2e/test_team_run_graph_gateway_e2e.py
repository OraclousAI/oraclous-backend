"""``GET /v1/engine/team-runs/{id}/graph`` — the new run-graph read, live through the gateway.

#1119/#1154: a screen needs to draw a run as a graph — every member, its status, WHY it did not
run, and the arrows between them — without leaking any payload. Commits 1-5 on this branch pin the
mechanism (``run_if`` skip-reason codes + the ``on_skip`` hook in ``packages/ohm``), the pure domain
derivation (``domain/run_graph.py``), the route's wire shape (a fake service through
``dependency_overrides``), the drive's persistence of ``member_skip_reasons``, and the column
round-trip. None of that proves the real HTTP wiring: the engine must actually RECORD a skip reason
during a real run, actually PERSIST it, and the route must actually READ it back — through the
deployed stack, via the gateway, per the DEPLOYED-STACK VERIFICATION LAW
(``FUCK_CLAUDE_FUCK_PAPERCLIP.md`` rule 5 / CLAUDE.md §9). This file is that proof.

RED until the route (``TeamRunGraphOut`` + ``TeamRunService.graph``), the ``domain/run_graph.py``
derivation, and the ``member_skip_reasons`` persistence land — today ``GET .../graph`` 404s as an
unmatched route. No custom backend logic stands in for that: real registration, real JWT, real
engine, real worker, real harness, no DB-direct assertions.

Two scenarios:

* ``test_run_graph_shows_skipped_and_completed_members_through_the_gateway`` (``byom``, needs a real
  model) — a 5-member reasoning-only team (scout -> summary, scout -> drafter, scout -> checker,
  drafter -> critic) built so THREE distinct ``run_if`` mechanisms fire in one run: a declared field
  absent from scout's dict output reads as ``None`` (``condition_false``, NOT
  ``condition_source_missing`` — the two are easy to swap and #1154 is explicit that they differ), a
  condition that cannot even be evaluated (``x in None`` raises ``TypeError`` in Python)
  (``condition_error``), and a condition whose tested member (drafter) was itself skipped, so its
  source is missing (``condition_source_missing``). Each agent echoes a per-run nonce so the run is
  provably real (fake mode cannot produce it) — and the nonce must appear in the existing full run
  read but never in the graph, proving "the graph carries pointers only, never payload" live.
* ``test_run_graph_marks_paused_gate_waiting_approval_and_cross_org_404`` (keyless, deterministic —
  runs in CI) — a book-shaped studio with an UN-approved human gate: the run lands and STAYS
  ``PAUSED``, and the graph must show the completed producer, the gate itself as
  ``waiting_approval``, and the not-yet-reached consumer as ``pending`` (never ``not_reached`` —
  the run is not terminal).

Both assert the cross-org 404 the graph read shares with ``/tree`` and ``/status``
(``test_team_run_gateway_e2e.py``'s two-user pattern).

Through the application-gateway on ``:8006`` only. Auto-skips when the gateway is down, and a skip
is not a pass.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_NODE_KEYS = {
    "role",
    "kind",
    "status",
    "error_code",
    "skip_reason",
    "reason_role",
    "input_from",
    "has_output",
    "loop",
    "fan_out",
}
_EDGE_KEYS = {"from", "to"}


def _poll(client: httpx.Client, run_id: str, until: set[str], tries: int = 30) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = client.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in until:
            return row
        time.sleep(2)
    raise AssertionError(f"run {run_id} never reached {until} (last: {row.get('state')})")


# ── scenario 1: a run_if DAG that exercises all three skip-reason mechanisms, real LLM ────────────

_USER_MODEL_KEY = os.environ.get("OPENROUTER_API_KEY")  # the user's own key, provided via env
requires_byom_key = pytest.mark.skipif(
    _USER_MODEL_KEY is None, reason="OPENROUTER_API_KEY not set (BYOM real-LLM run)"
)

#: declaration order only — the actual DAG shape below is patched onto every member explicitly
#: (see ``_CONDITIONAL_OVERRIDES``), never left to the importer's inferred sequential chain.
_CONDITIONAL_ROLES = ["scout", "summary", "drafter", "checker", "critic"]

#: The DAG this test needs, per role. ``scout`` never emits ``approved_for_draft`` — a declared
#: field absent from its dict output reads as ``None`` (drafter: condition_false). ``op: "in"``
#: against a literal ``value: null`` raises ``TypeError`` in Python regardless of what scout
#: produced, as long as scout produced SOMETHING (checker: condition_error). ``critic`` tests
#: drafter, which is itself skipped, so critic's condition SOURCE is missing (condition_source_
#: missing) — a different mechanism from drafter's own skip, even though both end up "skipped".
_CONDITIONAL_OVERRIDES: dict[str, dict[str, Any]] = {
    "scout": {"depends_on": []},
    "summary": {"depends_on": ["scout"]},
    "drafter": {
        "depends_on": ["scout"],
        "run_if": {"from_role": "scout", "field": "approved_for_draft", "op": "truthy"},
    },
    "checker": {
        "depends_on": ["scout"],
        "run_if": {"from_role": "scout", "op": "in", "value": None},
    },
    "critic": {
        "depends_on": ["drafter"],
        "run_if": {"from_role": "drafter", "op": "truthy"},
    },
}


def _conditional_studio(root: Path, nonce: str) -> None:
    """5 reasoning-only agents, each told to echo the per-run nonce (mirrors ``_echo_studio`` in
    ``test_team_byom_real_llm_gateway_e2e.py``) so a real LLM run is provably genuine. One agent
    per pipeline stage — the importer's own inferred depends_on from this shape is discarded and
    replaced wholesale by ``_patch_conditional_manifest``, so the stage numbering here only needs
    to produce 5 importable single-agent sub-harnesses, not the final DAG."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    body = f"Reply with exactly this token and nothing else: {nonce}"
    for role in _CONDITIONAL_ROLES:
        (agents / f"{role}.md").write_text(f"---\nname: {role}\nmodel: sonnet\n---\n{body}\n")
    for i, role in enumerate(_CONDITIONAL_ROLES, start=1):
        stage = root / "teams" / f"{i}-{role}"
        stage.mkdir(parents=True)
        stage_bump = i * 10  # keep charter numbering readable; not load-bearing
        stage.joinpath("charter.md").write_text(
            f"# Team {stage_bump} — {role}\n## Roster\n| Agent | Type | Model | Job |\n"
            f"| --- | --- | --- | --- |\n| `{role}` | subagent | sonnet | {role} |\n"
        )


def _patch_conditional_manifest(manifest_dict: dict[str, Any]) -> dict[str, Any]:
    """Every drafted member needs ``outputs_schema.required`` (F-NO-OUTPUT-CONTRACT,
    tests/e2e/README.md) — none of these members declare tools, so F-TOOL-UNJUSTIFIED needs nothing.
    ``depends_on``/``run_if`` are overridden per role to build the DAG this test targets."""
    for member in manifest_dict["members"]:
        member["outputs_schema"] = {"required": ["summary"]}
        member.update(_CONDITIONAL_OVERRIDES[member["role"]])
    return manifest_dict


def _expected_edges(manifest_dict: dict[str, Any]) -> set[tuple[str, str]]:
    """Computed from the SAME patched manifest the run is created with — never hand-derived."""
    return {
        (dep, member["role"]) for member in manifest_dict["members"] for dep in member["depends_on"]
    }


def _byom_model(credential_id: str) -> dict[str, Any]:
    """The user's own model binding — a cheap OpenRouter model resolved via their credential."""
    return {
        "role": "primary",
        "binding": os.environ["E2E_MODEL"],
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id},
    }


@requires_byom_key
@pytest.mark.byom  # the full real-LLM leg (nightly) — NOT byom_smoke, not part of the PR-gate five
def test_run_graph_shows_skipped_and_completed_members_through_the_gateway(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
    assert_run_succeeded: Callable[..., None],
) -> None:
    user = register(f"graphuser{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])

    # 1) the user stores THEIR OWN model token via the real credential API (never server-side)
    cred = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "my openrouter model",
            "provider": "openrouter",
            "cred_type": "api_key",
            "credential": {"api_key": _USER_MODEL_KEY},
        },
    )
    assert cred.status_code == 201, cred.text
    credential_id = cred.json()["id"]

    # 2) import 5 real single-agent sub-harnesses, then patch the manifest's DAG + point every
    # member's model at the user's own credential
    nonce = uuid.uuid4().hex[:10]
    _conditional_studio(tmp_path, nonce)
    imported = import_setup(
        tmp_path, owner_organization_id=uuid.uuid4(), name="conditional studio", substrate="file"
    )
    assert imported.manifest is not None
    assert set(imported.sub_harnesses) == set(_CONDITIONAL_ROLES)
    manifest_dict = _patch_conditional_manifest(imported.manifest.model_dump(mode="json"))
    sub_harnesses = {role: dict(sub) for role, sub in imported.sub_harnesses.items()}
    for sub in sub_harnesses.values():
        sub["models"] = [_byom_model(credential_id)]  # the user's BYOM model, per member

    # 3) run the team THROUGH THE GATEWAY — real engine -> worker -> live harness -> real OpenRouter
    created = c.post(
        "/v1/engine/team-runs",
        json={"manifest": manifest_dict, "sub_harnesses": sub_harnesses, "gate_decisions": {}},
    )
    assert created.status_code == 202, created.text  # the worker drives it; request didn't block
    run_id = created.json()["id"]

    done = _poll(c, run_id, {"SUCCEEDED", "FAILED", "REJECTED"})
    # ADR-042: SUCCEEDED iff no member is failed/blocked — a skipped member is neither, so 3 real
    # deliveries + 2 skips still settles SUCCEEDED. assert_run_succeeded distinguishes a genuine
    # product failure from a provider refusal (429/5xx/timeout) so a flaky model run fails legibly.
    assert_run_succeeded(done, state_key="state")
    assert set(done["results"]) == {"scout", "summary"}  # drafter/checker/critic never dispatched
    assert nonce in str(done["results"]), (
        f"nonce {nonce!r} not in team results — is the harness LIVE? results={done['results']!r}"
    )
    assert done["simulated"] is False  # #907: a real BYOM run must not read as the scripted proxy

    # 4) the new read — through the gateway, nothing internal
    graph_resp = c.get(f"/v1/engine/team-runs/{run_id}/graph")
    assert graph_resp.status_code == 200, graph_resp.text
    graph = graph_resp.json()

    assert set(graph.keys()) == {"team_run_id", "state", "nodes", "edges"}
    assert graph["team_run_id"] == run_id
    assert graph["state"] == done["state"]

    assert len(graph["nodes"]) == len(_CONDITIONAL_ROLES)
    for node in graph["nodes"]:
        assert set(node.keys()) == _NODE_KEYS, node
    for edge in graph["edges"]:
        assert set(edge.keys()) == _EDGE_KEYS, edge

    by_role = {n["role"]: n for n in graph["nodes"]}
    assert by_role["scout"]["status"] == "succeeded"
    assert by_role["summary"]["status"] == "succeeded"

    assert by_role["drafter"]["status"] == "skipped"
    assert by_role["drafter"]["skip_reason"] == "condition_false"
    assert by_role["drafter"]["reason_role"] == "scout"

    assert by_role["checker"]["status"] == "skipped"
    assert by_role["checker"]["skip_reason"] == "condition_error"
    assert by_role["checker"]["reason_role"] == "scout"

    assert by_role["critic"]["status"] == "skipped"
    assert by_role["critic"]["skip_reason"] == "condition_source_missing"
    assert by_role["critic"]["reason_role"] == "drafter"

    edge_pairs = {(e["from"], e["to"]) for e in graph["edges"]}
    assert edge_pairs == _expected_edges(manifest_dict)  # computed, never hand-derived
    assert len(graph["edges"]) == len(edge_pairs)  # de-duplicated, no accidental repeats

    # 5) the full run read still carries the nonce; the graph never does — pointers only, never
    # payload (#1154: "the graph carries pointers only... this endpoint exposes nothing new")
    full_run = c.get(f"/v1/engine/team-runs/{run_id}")
    assert full_run.status_code == 200, full_run.text
    assert nonce in str(full_run.json()), "the existing run read lost the nonce"
    assert nonce not in graph_resp.text, "the graph leaked member output"

    # 6) cross-org 404 — a second user cannot read this run's graph either (mirrors /tree, /status)
    intruder = gateway_client(register(f"graphintruder{uuid.uuid4().hex[:10]} user")["token"])
    assert intruder.get(f"/v1/engine/team-runs/{run_id}/graph").status_code == 404


# ── scenario 2: a paused human gate, keyless, deterministic (runs in CI) ──────────────────────────


def _book_studio(root: Path) -> None:
    """A book-shaped studio: researcher -> [Gate A blocks] -> writer, reasoning-only so it runs on
    the deployed harness without E5 tool resolution. Mirrors ``_book_studio`` in
    ``test_team_run_gateway_e2e.py`` byte-for-byte (house convention: each e2e module keeps its own
    copy of a small fixture-builder rather than importing across test modules)."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "researcher.md").write_text(
        "---\nname: researcher\nmodel: sonnet\n---\nResearch the topic and propose an outline.\n"
    )
    (agents / "writer.md").write_text(
        "---\nname: writer\nmodel: sonnet\n---\nDraft the chapter from the approved outline.\n"
    )
    (root / "teams" / "1-research").mkdir(parents=True)
    (root / "teams" / "1-research" / "charter.md").write_text(
        "# Team I — Research\n## Roster\n"
        "| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `researcher` | subagent | sonnet | research |\n"
        "## Hard gates\n- **Gate A** — the author approves the outline before drafting.\n"
    )
    (root / "teams" / "2-write").mkdir(parents=True)
    (root / "teams" / "2-write" / "charter.md").write_text(
        "# Team II — Write\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `writer` | subagent | sonnet | draft |\n"
    )


def test_run_graph_marks_paused_gate_waiting_approval_and_cross_org_404(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """Unlike ``test_book_studio_runs_through_the_gateway_with_a_blocking_gate`` (which pre-approves
    the gate to prove the full run completes), this run's gate is left undecided on purpose — the
    run lands and STAYS ``PAUSED``, so the graph must show a not-yet-reached member as ``pending``
    (the run isn't terminal), never ``not_reached`` (reserved for a FINISHED run)."""
    _book_studio(tmp_path)
    imported = import_setup(
        tmp_path, owner_organization_id=uuid.uuid4(), name="book studio", substrate="file"
    )
    assert imported.manifest is not None
    body = {
        "manifest": imported.manifest.model_dump(mode="json"),
        "sub_harnesses": imported.sub_harnesses,
        "gate_decisions": {},  # deliberately NOT pre-approved — the run must stay PAUSED
    }
    c = gateway_client(register(f"graphgateowner{uuid.uuid4().hex[:10]} user")["token"])
    created = c.post("/v1/engine/team-runs", json=body)
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]

    paused = _poll(c, run_id, {"PAUSED", "SUCCEEDED", "FAILED"})
    assert paused["state"] == "PAUSED"  # the human gate blocks, undecided
    assert paused["paused_at"] == ["gate-a"]

    graph_resp = c.get(f"/v1/engine/team-runs/{run_id}/graph")
    assert graph_resp.status_code == 200, graph_resp.text
    graph = graph_resp.json()

    assert graph["team_run_id"] == run_id
    assert graph["state"] == "PAUSED"
    for node in graph["nodes"]:
        assert set(node.keys()) == _NODE_KEYS, node

    by_role = {n["role"]: n for n in graph["nodes"]}
    assert by_role["researcher"]["status"] == "succeeded"
    assert by_role["gate-a"]["kind"] == "human"
    assert by_role["gate-a"]["status"] == "waiting_approval"
    assert by_role["writer"]["status"] == "pending"  # not "not_reached" — the run is still live

    intruder = gateway_client(register(f"graphgateintruder{uuid.uuid4().hex[:10]} user")["token"])
    assert intruder.get(f"/v1/engine/team-runs/{run_id}/graph").status_code == 404
