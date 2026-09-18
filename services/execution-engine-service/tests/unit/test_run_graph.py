"""``derive_run_graph`` (domain layer) — the pure derivation behind the new read, #1119/#1154.

``GET /v1/engine/team-runs/{id}/graph`` is a read-only projection of a run's team definition plus
its stored run fields (``member_status``, ``member_error_codes``, ``member_skip_reasons``,
``results``, ``paused_at``) onto the frontend's node/edge shape — never a second source of truth,
never a live re-validation of the manifest. Modelled directly on ``domain/outcome_blockers.py``
(read it first): pure, never-raising, fail-closed to an empty graph on a malformed manifest.

The full, ruled response contract lives on #1154 (copied to
``oraclous-knowledge/flows/interface-contracts.md``); this file pins it. Two mechanisms are
verified against the ACTUAL code rather than the issue's summary, per the #1119 task brief:

  * ``rejected`` status: ``packages/ohm/orchestrate.py`` sets ``status="rejected"`` with
    ``paused_at=rejected`` (the rejected gate role(s)), and ``team_run_service.py`` persists
    ``paused_at=list(result.paused_at)`` on EVERY settle, REJECTED included (not just PAUSED). So a
    REJECTED run's ``paused_at`` really does still name the gate role — the same field
    ``waiting_approval`` reads on a PAUSED run, disambiguated only by ``state``. No divorced-from-
    reality pin needed here; see ``test_rejected_gate_is_rejected``.
  * ``blocked``'s ``reason_role``: ``orchestrate.py``'s ``_blocked_by_upstream`` only asks
    "is ANY dependency faulted" (``any(...)``), it does not itself track *which* one. The domain
    module derives the FIRST dependency (in declared ``depends_on`` order) that is undelivered
    (failed/blocked, or absent from ``results``) — see
    ``test_blocked_names_first_undelivered_upstream``, which deliberately puts the undelivered
    dependency SECOND in ``depends_on`` to prove this is "first undelivered", not "first listed".

RED until ``domain/run_graph.py`` lands. The seam is imported function-locally on purpose
(`.claude/rules/tests-seam-imports.md`): a module-level import of a not-yet-built ``oraclous_*``
seam aborts collection for the whole run and reddens every open PR.

Naming note for the implementer: ``from`` is a Python keyword and cannot be a dataclass field
name, so ``RunGraphEdge`` uses ``from_`` (mirrors the standard ``class_``/``id_``-style keyword-
collision convention) for the pure domain shape here. The pydantic API schema (commit 3, the
routes-layer twin, out of scope for this file) is where ``from_`` gets a ``Field(alias="from")``
so the wire JSON still says ``from`` per #1154 — this dataclass is never serialized directly.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_VERDICT_ESCALATION_ROLE = "__verdict_escalation__"
# a fixed identity default for ``_derive``'s ``team_run_id`` kwarg, so every existing call site in
# this file keeps working unchanged (#1154 ruling 1: ``RunGraph`` carries ``team_run_id``/``state``
# directly, mirroring ``TeamRunStatus``, ``team_run_service.py:443``).
_RUN_ID = uuid.uuid4()
# the file's two live/terminal state splits, shared by the absent-member tests (section 3) and the
# unrecognized-stored-status tests (section 3b), so the classification is never hand-copied twice.
_LIVE_STATES = ["QUEUED", "RUNNING", "PAUSED"]
_TERMINAL_STATES = ["SUCCEEDED", "FAILED", "REJECTED", "COST_BUDGET"]


def _member(role: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:x/{role}@1",
        "depends_on": [],
    }
    base.update(over)
    return base


def _manifest(members: list[dict[str, Any]], **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ohm_version": "1.1",
        "metadata": {
            "id": "24869709-764a-49d0-b259-ce561340a8ec",
            "name": "t",
            "owner_organization_id": "00000000-0000-0000-0000-0000000000a0",
            "kind": "team",
        },
        "members": members,
        "runtime": {"entrypoint": members[0]["role"] if members else "none"},
    }
    base.update(over)
    return base


def _derive(**kwargs: Any) -> Any:
    from oraclous_execution_engine_service.domain.run_graph import derive_run_graph

    defaults: dict[str, Any] = {
        "manifest": None,
        "team_run_id": _RUN_ID,
        "state": "RUNNING",
        "member_status": None,
        "member_error_codes": None,
        "member_skip_reasons": None,
        "results": None,
        "paused_at": None,
    }
    defaults.update(kwargs)
    return derive_run_graph(**defaults)


def _node(graph: Any, role: str) -> Any:
    for node in graph.nodes:
        if node.role == role:
            return node
    raise AssertionError(f"no node for role {role!r} in {graph.nodes!r}")


# ── 0. team_run_id / state pass straight through ────────────────────────────


def test_team_run_id_and_state_pass_through_unchanged() -> None:
    """``RunGraph`` carries ``team_run_id``/``state`` directly (#1154 ruling 1), mirroring
    ``TeamRunStatus`` (``team_run_service.py:443``), so the route can map them onto
    ``TeamRunGraphOut`` without recomputing or re-deriving either value."""
    run_id = uuid.uuid4()

    graph = _derive(team_run_id=run_id, state="PAUSED", manifest=_manifest([]))

    assert graph.team_run_id == run_id
    assert graph.state == "PAUSED"


# ── 1. ordering ────────────────────────────────────────────────────────────


def test_nodes_follow_declaration_order_edges_follow_depends_on() -> None:
    from oraclous_execution_engine_service.domain.run_graph import RunGraphEdge

    members = [
        _member("writer", depends_on=["researcher"]),
        _member("researcher", depends_on=[]),
        # "writer" is repeated and "ghost" names no declared member (#1154, B3): the edge set
        # must still de-duplicate the repeat and silently drop the dangling dependency, never
        # raise and never draw a second writer->editor edge or a ghost edge.
        _member("editor", depends_on=["writer", "researcher", "writer", "ghost"]),
    ]
    manifest = _manifest(members)

    graph = _derive(manifest=manifest, state="RUNNING")

    assert [n.role for n in graph.nodes] == ["writer", "researcher", "editor"]
    expected_edges = {
        RunGraphEdge(from_="researcher", to="writer"),
        RunGraphEdge(from_="writer", to="editor"),
        RunGraphEdge(from_="researcher", to="editor"),
    }
    assert set(graph.edges) == expected_edges
    assert len(graph.edges) == 3  # real de-duplication, not set-equality hiding a duplicate
    assert all(e.from_ != "ghost" and e.to != "ghost" for e in graph.edges)


# ── 2. stored status mapping ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("running", "running"),
        ("succeeded", "succeeded"),
        ("partial", "partial"),
        ("failed", "failed"),
        ("blocked", "blocked"),
        ("skipped", "skipped"),
        ("budget_skipped", "budget_skipped"),
        ("re_task", "pending"),
    ],
)
def test_stored_status_mapping(stored: str, expected: str) -> None:
    manifest = _manifest([_member("solo")])

    graph = _derive(
        manifest=manifest,
        state="RUNNING",
        member_status={"solo": stored},
    )

    assert _node(graph, "solo").status == expected


# ── 3. absent status ──────────────────────────────────────────────────────


@pytest.mark.parametrize("state", _LIVE_STATES)
def test_absent_member_pending_on_live_run(state: str) -> None:
    manifest = _manifest([_member("solo")])

    graph = _derive(manifest=manifest, state=state, member_status={})

    assert _node(graph, "solo").status == "pending"


@pytest.mark.parametrize("state", _TERMINAL_STATES)
def test_absent_member_not_reached_on_terminal(state: str) -> None:
    manifest = _manifest([_member("solo")])

    graph = _derive(manifest=manifest, state=state, member_status={})

    assert _node(graph, "solo").status == "not_reached"


# ── 3b. unrecognized stored status ───────────────────────────────────────────
# #1154 ruling (item 10): a stored ``member_status`` value outside the contract's listed values
# behaves EXACTLY like no recorded status at all — never a 500, never surfaced verbatim.


@pytest.mark.parametrize("state", _LIVE_STATES)
def test_unrecognized_stored_status_pending_on_live_run(state: str) -> None:
    manifest = _manifest([_member("solo")])

    graph = _derive(
        manifest=manifest,
        state=state,
        member_status={"solo": "some_bogus_value_never_in_the_contract"},
    )

    assert _node(graph, "solo").status == "pending"


@pytest.mark.parametrize("state", _TERMINAL_STATES)
def test_unrecognized_stored_status_not_reached_on_terminal(state: str) -> None:
    manifest = _manifest([_member("solo")])

    graph = _derive(
        manifest=manifest,
        state=state,
        member_status={"solo": "some_bogus_value_never_in_the_contract"},
    )

    assert _node(graph, "solo").status == "not_reached"


# ── 4. waiting_approval ───────────────────────────────────────────────────


def test_paused_gate_is_waiting_approval() -> None:
    manifest = _manifest(
        [
            _member("writer", depends_on=[]),
            _member("gate", kind="human", human_role="reviewer", depends_on=["writer"]),
        ]
    )

    graph = _derive(
        manifest=manifest,
        state="PAUSED",
        # the gate carries a STALE recorded status ("succeeded" for a human gate that has not
        # actually run) alongside its presence in paused_at: the paused_at check (steps 1-2) must
        # win over the recorded-status branch (step 3), never the other way round (#1154).
        member_status={"writer": "succeeded", "gate": "succeeded"},
        paused_at=["gate"],
    )

    assert _node(graph, "gate").status == "waiting_approval"
    # a member that is not the paused-on gate keeps its own recorded status
    assert _node(graph, "writer").status == "succeeded"


# ── 5. sentinel role never surfaces ───────────────────────────────────────


def test_verdict_escalation_sentinel_is_not_a_node() -> None:
    manifest = _manifest([_member("solo")])

    graph = _derive(
        manifest=manifest,
        state="PAUSED",
        member_status={"solo": "succeeded"},
        paused_at=[_VERDICT_ESCALATION_ROLE],
    )

    assert all(n.role != _VERDICT_ESCALATION_ROLE for n in graph.nodes)
    # the sentinel does not name a real member, so it must not spuriously flip "solo" either
    assert _node(graph, "solo").status == "succeeded"


# ── 6. rejected gate ──────────────────────────────────────────────────────


def test_rejected_gate_is_rejected() -> None:
    """Mirrors the actual settle in ``orchestrate.py``: a REJECTED run's ``paused_at`` still names
    the rejected gate role (``status="rejected", paused_at=rejected``), persisted verbatim by
    ``team_run_service.py``'s ``paused_at=list(result.paused_at)`` on every settle. A member with no
    recorded status on a REJECTED (terminal) run is ``not_reached``, never ``rejected`` itself."""
    manifest = _manifest(
        [
            _member("writer", depends_on=[]),
            _member("gate", kind="human", human_role="reviewer", depends_on=["writer"]),
        ]
    )

    graph = _derive(
        manifest=manifest,
        state="REJECTED",
        # same STALE-recorded-status guard as the PAUSED case above: paused_at naming the
        # rejected gate (steps 1-2) must win over the gate's own stale recorded status (step 3).
        member_status={"writer": "succeeded", "gate": "succeeded"},
        paused_at=["gate"],
    )

    assert _node(graph, "gate").status == "rejected"
    assert _node(graph, "writer").status == "succeeded"


# ── 7-9. skip_reason ──────────────────────────────────────────────────────


@pytest.mark.parametrize("code", ["condition_false", "condition_source_missing", "condition_error"])
def test_skipped_member_carries_recorded_code_and_role(code: str) -> None:
    manifest = _manifest(
        [
            _member("researcher", depends_on=[]),
            _member("writer", depends_on=["researcher"]),
        ]
    )

    graph = _derive(
        manifest=manifest,
        state="SUCCEEDED",
        member_status={"researcher": "succeeded", "writer": "skipped"},
        member_skip_reasons={"writer": {"code": code, "role": "researcher"}},
    )

    node = _node(graph, "writer")
    assert node.status == "skipped"
    assert node.skip_reason == code
    assert node.reason_role == "researcher"


def test_skipped_without_record_is_unrecorded() -> None:
    manifest = _manifest([_member("writer")])

    graph = _derive(
        manifest=manifest,
        state="SUCCEEDED",
        member_status={"writer": "skipped"},
        member_skip_reasons={},
    )

    node = _node(graph, "writer")
    assert node.status == "skipped"
    assert node.skip_reason == "unrecorded"
    assert node.reason_role is None


def test_stale_reason_on_non_skipped_member_is_ignored() -> None:
    """A role re-ran and succeeded after an earlier skip; the leftover skip-reason record must not
    surface (#1154: "a reason saved against a member that is no longer skipped is ignored")."""
    manifest = _manifest([_member("writer")])

    graph = _derive(
        manifest=manifest,
        state="SUCCEEDED",
        member_status={"writer": "succeeded"},
        member_skip_reasons={"writer": {"code": "condition_false", "role": "researcher"}},
    )

    node = _node(graph, "writer")
    assert node.status == "succeeded"
    assert node.skip_reason is None
    assert node.reason_role is None


# ── 10. blocked ───────────────────────────────────────────────────────────


def test_blocked_names_first_undelivered_upstream() -> None:
    """``depends_on`` order is ["researcher", "reviewer"]; "researcher" DELIVERED (succeeded, has a
    result) and "reviewer" FAILED. The blocked consumer must name "reviewer" — the first member
    that did not deliver — not simply ``depends_on[0]``."""
    manifest = _manifest(
        [
            _member("researcher", depends_on=[]),
            _member("reviewer", depends_on=[]),
            _member("writer", depends_on=["researcher", "reviewer"]),
        ]
    )

    graph = _derive(
        manifest=manifest,
        state="FAILED",
        member_status={"researcher": "succeeded", "reviewer": "failed", "writer": "blocked"},
        results={"researcher": {"summary": "ok"}},
    )

    node = _node(graph, "writer")
    assert node.status == "blocked"
    assert node.skip_reason == "upstream_not_delivered"
    assert node.reason_role == "reviewer"


def test_blocked_names_first_when_first_dep_is_the_undelivered_one() -> None:
    manifest = _manifest(
        [
            _member("a", depends_on=[]),
            _member("b", depends_on=[]),
            _member("consumer", depends_on=["a", "b"]),
        ]
    )

    graph = _derive(
        manifest=manifest,
        state="FAILED",
        member_status={"a": "failed", "b": "succeeded", "consumer": "blocked"},
        results={"b": {"summary": "ok"}},
    )

    assert _node(graph, "consumer").reason_role == "a"


# ── 11. budget_skipped ────────────────────────────────────────────────────


def test_budget_skipped_is_budget_exhausted() -> None:
    manifest = _manifest([_member("writer")])

    graph = _derive(
        manifest=manifest,
        state="COST_BUDGET",
        member_status={"writer": "budget_skipped"},
    )

    node = _node(graph, "writer")
    assert node.status == "budget_skipped"
    assert node.skip_reason == "budget_exhausted"
    assert node.reason_role is None


# ── 12. error_code ────────────────────────────────────────────────────────


def test_error_code_only_on_failed() -> None:
    manifest = _manifest(
        [
            _member("failer"),
            _member("succeeder"),
        ]
    )

    graph = _derive(
        manifest=manifest,
        state="FAILED",
        member_status={"failer": "failed", "succeeder": "succeeded"},
        member_error_codes={"failer": "TOOL_TIMEOUT", "succeeder": "TOOL_TIMEOUT"},
    )

    assert _node(graph, "failer").error_code == "TOOL_TIMEOUT"
    # an error code stored against a non-failed member (stale/unused) must never leak through
    assert _node(graph, "succeeder").error_code is None


# ── 13. input_from / has_output ───────────────────────────────────────────


def test_input_from_and_has_output() -> None:
    manifest = _manifest(
        [
            _member("researcher", depends_on=[]),
            _member("silent_dep", depends_on=[]),
            _member("writer", depends_on=["researcher", "silent_dep"]),
        ]
    )

    graph = _derive(
        manifest=manifest,
        state="RUNNING",
        member_status={"researcher": "succeeded", "silent_dep": "succeeded", "writer": "running"},
        results={"researcher": {"summary": "ok"}, "silent_dep": None},
    )

    writer = _node(graph, "writer")
    assert writer.input_from == ["researcher"]
    assert writer.has_output is False

    researcher = _node(graph, "researcher")
    assert researcher.has_output is True
    assert researcher.input_from == []

    silent_dep = _node(graph, "silent_dep")
    assert silent_dep.has_output is False


def test_input_from_follows_depends_on_order_not_declaration_order() -> None:
    # Two producing dependencies, declared first_declared -> second_declared, but the consumer
    # lists them the other way round: input_from must follow the consumer's depends_on order.
    depends_on = ["second_declared", "first_declared"]
    manifest = _manifest(
        [
            _member("first_declared", depends_on=[]),
            _member("second_declared", depends_on=[]),
            _member("writer", depends_on=depends_on),
        ]
    )

    graph = _derive(
        manifest=manifest,
        state="RUNNING",
        member_status={
            "first_declared": "succeeded",
            "second_declared": "succeeded",
            "writer": "running",
        },
        results={"first_declared": {"summary": "a"}, "second_declared": {"summary": "b"}},
    )

    assert _node(graph, "writer").input_from == depends_on


# ── 14. loop / fan_out ────────────────────────────────────────────────────


def test_loop_index_and_fan_out_flag() -> None:
    manifest = _manifest(
        [
            _member("intro", depends_on=[]),
            _member("drafter", depends_on=["intro"]),
            _member("critic", depends_on=["drafter"], fan_out={"over": "items"}),
            _member("outro", depends_on=["critic"]),
        ],
        orchestration={
            "loops": [
                {"members": ["drafter", "critic"], "routing": {}},
            ]
        },
    )

    graph = _derive(manifest=manifest, state="RUNNING")

    assert _node(graph, "drafter").loop == 0
    assert _node(graph, "critic").loop == 0
    assert _node(graph, "intro").loop is None
    assert _node(graph, "outro").loop is None

    assert _node(graph, "critic").fan_out is True
    assert _node(graph, "drafter").fan_out is False


def test_edges_inside_a_loop_are_not_drawn() -> None:
    """Loop membership groups drafter/critic via ``loop`` instead of an edge between them, even
    though critic ``depends_on`` drafter."""
    from oraclous_execution_engine_service.domain.run_graph import RunGraphEdge

    manifest = _manifest(
        [
            _member("intro", depends_on=[]),
            _member("drafter", depends_on=["intro"]),
            _member("critic", depends_on=["drafter"]),
        ],
        orchestration={"loops": [{"members": ["drafter", "critic"], "routing": {}}]},
    )

    graph = _derive(manifest=manifest, state="RUNNING")

    assert RunGraphEdge(from_="drafter", to="critic") not in graph.edges
    assert RunGraphEdge(from_="intro", to="drafter") in graph.edges


# ── 15. every key present ─────────────────────────────────────────────────


def test_every_node_key_present_when_null() -> None:
    manifest = _manifest([_member("bare")])

    graph = _derive(manifest=manifest, state="QUEUED")

    node = _node(graph, "bare")
    as_dict = dataclasses.asdict(node)
    for key in (
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
    ):
        assert key in as_dict, f"missing key {key!r}"
    assert as_dict["role"] == "bare"
    assert as_dict["kind"] == "agent"
    assert as_dict["status"] == "pending"
    assert as_dict["error_code"] is None
    assert as_dict["skip_reason"] is None
    assert as_dict["reason_role"] is None
    assert as_dict["input_from"] == []
    assert as_dict["has_output"] is False
    assert as_dict["loop"] is None
    assert as_dict["fan_out"] is False


# ── 16. malformed manifest ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "manifest",
    [None, {}, {"members": "not-a-list"}],
    ids=["none", "empty-dict", "members-not-a-list"],
)
def test_malformed_manifest_yields_empty_graph(manifest: Any) -> None:
    graph = _derive(manifest=manifest, state="RUNNING")

    assert graph.nodes == []
    assert graph.edges == []


# ── 17. no payload/condition leakage ──────────────────────────────────────


def test_no_payload_or_condition_value_reaches_the_graph() -> None:
    sentinel = "SENTINEL-do-not-leak-4f6c2b"
    manifest = _manifest(
        [
            # #1154: "never leak ... a member's instructions, description" — plant the sentinel on
            # the member itself, not only inside a run_if condition.
            _member("researcher", depends_on=[], subgoal=sentinel, description=sentinel),
            _member(
                "writer",
                depends_on=["researcher"],
                run_if={
                    "from_role": "researcher",
                    # the compared FIELD NAME must never leak either, not only the compared value
                    "field": sentinel,
                    "op": "gte",
                    "value": sentinel,
                },
            ),
        ]
    )

    graph = _derive(
        manifest=manifest,
        state="SUCCEEDED",
        member_status={"researcher": "succeeded", "writer": "skipped"},
        member_skip_reasons={"writer": {"code": "condition_false", "role": "researcher"}},
        results={"researcher": {"summary": sentinel}},
        member_error_codes={"researcher": sentinel},
    )

    flattened = repr(dataclasses.asdict(graph))
    assert sentinel not in flattened
    # prove this is a REAL, populated graph that correctly resolved the sentinel-bearing member —
    # not a stub returning an empty RunGraph(nodes=[], edges=[]), which would trivially pass the
    # assertion above too.
    assert _node(graph, "writer").skip_reason == "condition_false"
