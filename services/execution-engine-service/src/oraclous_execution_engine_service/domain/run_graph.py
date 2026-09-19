"""``derive_run_graph`` (domain layer) — the pure derivation behind the read-only run graph,
#1119/#1154, ``GET /v1/engine/team-runs/{id}/graph``.

Projects a run's team definition (the stored manifest snapshot) plus its stored run fields
(``member_status``, ``member_error_codes``, ``member_skip_reasons``, ``results``, ``paused_at``)
onto a node/edge graph for the frontend — never a second source of truth, never a live
re-validation of the manifest. Modelled directly on ``domain/outcome_blockers.py``: pure,
never-raising, fail-closed to an empty graph on a malformed manifest.

The full, ruled response contract lives on #1154 (copied to
``oraclous-knowledge/flows/interface-contracts.md`` as §RUN-GRAPH); this file pins it.

Never reads a member's ``run_if``, ``subgoal``, ``description``, tools, output *values*, or error
*text* — only presence/absence (``results.get(role) is not None``) and the small closed set of
codes already recorded structurally (``member_skip_reasons``, ``member_error_codes``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from oraclous_execution_engine_service.domain.outcome_blockers import derive_outcome_blockers

NodeKind = Literal["agent", "human"]
NodeStatus = Literal[
    "waiting_approval",
    "rejected",
    "running",
    "succeeded",
    "partial",
    "failed",
    "blocked",
    "skipped",
    "budget_skipped",
    "pending",
    "not_reached",
]
SkipReason = Literal[
    "condition_false",
    "condition_source_missing",
    "condition_error",
    "unrecorded",
    "upstream_not_delivered",
    "budget_exhausted",
]

#: the three run_if verdict codes the orchestrator itself records (packages/ohm/orchestrate.py);
#: any other value in a stored ``member_skip_reasons`` entry is treated as unrecorded.
_RECORDED_CODES: tuple[str, ...] = (
    "condition_false",
    "condition_source_missing",
    "condition_error",
)

#: states in which a member with no recorded status yet still has a turn coming.
_LIVE_STATES = {"QUEUED", "RUNNING", "PAUSED"}

#: pass-through stored statuses, plus ``re_task`` (mid-drive bookkeeping the graph read never
#: exposes verbatim — it reads as still-pending work).
_RECORDED_STATUS: dict[str, NodeStatus] = {
    "running": "running",
    "succeeded": "succeeded",
    "partial": "partial",
    "failed": "failed",
    "blocked": "blocked",
    "skipped": "skipped",
    "budget_skipped": "budget_skipped",
    "re_task": "pending",
}

#: duplicates ``team_run_service._VERDICT_ESCALATION_ROLE`` on purpose: this domain module may not
#: import from ``services/`` (layering), and the sentinel is a fixed string, not a live seam.
VERDICT_ESCALATION_ROLE = "__verdict_escalation__"


@dataclass(frozen=True)
class RunGraphEdge:
    """A dependency edge, ``from_`` -> ``to``. ``from`` is a Python keyword; the wire alias lives
    on the schema DTO (``schema.engine_schemas.RunGraphEdgeOut``), not here."""

    from_: str
    to: str


@dataclass(frozen=True)
class RunGraphNode:
    """One team member, as the graph read reports it. Every field is always present (even when
    null) — a consumer must be able to tell "no reason recorded" from "field omitted"."""

    role: str
    kind: NodeKind
    status: NodeStatus
    error_code: str | None
    skip_reason: SkipReason | None
    reason_role: str | None
    input_from: list[str]
    has_output: bool
    loop: int | None
    fan_out: bool


@dataclass(frozen=True)
class RunGraph:
    """The full read: identity fields pass straight through the stored run row, unchanged."""

    team_run_id: uuid.UUID
    state: str
    nodes: list[RunGraphNode]
    edges: list[RunGraphEdge]


def _declared_deps(raw: dict[str, Any]) -> list[str]:
    """A member's own ``depends_on``, string entries only, in declared order (may repeat or name
    a role that does not exist — callers filter that)."""
    deps = raw.get("depends_on")
    if not isinstance(deps, list):
        return []
    return [d for d in deps if isinstance(d, str)]


def _dep_undelivered(
    dep: str,
    *,
    statuses: Mapping[str, NodeStatus],
    outcome_faulted: set[str],
    member_status: Mapping[str, Any],
    results: Mapping[str, Any],
) -> bool:
    """Whether ``dep`` counts as "did not deliver" for a blocked consumer's reason: it failed or
    was itself blocked; it settled but lost its own declared outcome (#834); or it never got a
    recorded status at all and produced no result (still pending/not_reached upstream)."""
    dep_status = statuses.get(dep)
    if dep_status in ("failed", "blocked"):
        return True
    if dep_status in ("succeeded", "partial") and dep in outcome_faulted:
        return True
    raw_status = member_status.get(dep)
    has_recorded_status = isinstance(raw_status, str) and raw_status in _RECORDED_STATUS
    return not has_recorded_status and results.get(dep) is None


def derive_run_graph(
    *,
    manifest: Any,
    team_run_id: uuid.UUID,
    state: str,
    member_status: Mapping[str, Any] | None,
    member_error_codes: Mapping[str, Any] | None,
    member_skip_reasons: Mapping[str, Any] | None,
    results: Mapping[str, Any] | None,
    paused_at: Sequence[Any] | None,
) -> RunGraph:
    """The run's graph — an empty one on a malformed manifest, never raising."""
    empty = RunGraph(team_run_id=team_run_id, state=state, nodes=[], edges=[])
    if not isinstance(manifest, dict):
        return empty
    raw_members = manifest.get("members")
    if not isinstance(raw_members, list):
        return empty

    members: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for raw in raw_members:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        if not isinstance(role, str) or not role or role in members:
            continue
        members[role] = raw
        order.append(role)

    member_status_map: Mapping[str, Any] = (
        member_status if isinstance(member_status, Mapping) else {}
    )
    member_error_codes_map: Mapping[str, Any] = (
        member_error_codes if isinstance(member_error_codes, Mapping) else {}
    )
    member_skip_reasons_map: Mapping[str, Any] = (
        member_skip_reasons if isinstance(member_skip_reasons, Mapping) else {}
    )
    results_map: Mapping[str, Any] = results if isinstance(results, Mapping) else {}

    waiting = {v for v in (paused_at or []) if isinstance(v, str)} - {VERDICT_ESCALATION_ROLE}

    loop_of: dict[str, int] = {}
    orchestration = manifest.get("orchestration")
    if isinstance(orchestration, dict):
        loops = orchestration.get("loops")
        if isinstance(loops, list):
            for i, loop in enumerate(loops):
                if not isinstance(loop, dict):
                    continue
                loop_members = loop.get("members")
                if not isinstance(loop_members, list):
                    continue
                for lm in loop_members:
                    if isinstance(lm, str):
                        loop_of.setdefault(lm, i)

    outcome_faulted = {
        blocker.role
        for blocker in derive_outcome_blockers(
            results=cast("dict[str, Any]", dict(results_map)),
            member_status=cast("dict[str, str]", dict(member_status_map)),
            manifest=manifest,
        )
    }

    is_live = state in _LIVE_STATES

    # ── pass A: kind + status for every member, needed up front so a blocked member can name
    # the first dependency that did not deliver, by that dependency's OWN derived status. ──
    kinds: dict[str, NodeKind] = {}
    statuses: dict[str, NodeStatus] = {}
    for role in order:
        raw = members[role]
        kind: NodeKind = "human" if raw.get("kind") == "human" else "agent"
        kinds[role] = kind
        in_waiting = role in waiting
        status: NodeStatus
        if kind == "human" and in_waiting and state == "PAUSED":
            status = "waiting_approval"
        elif kind == "human" and in_waiting and state == "REJECTED":
            status = "rejected"
        else:
            stored = member_status_map.get(role)
            if isinstance(stored, str) and stored in _RECORDED_STATUS:
                status = _RECORDED_STATUS[stored]
            else:
                status = "pending" if is_live else "not_reached"
        statuses[role] = status

    # ── pass B: everything else, per node ──
    nodes: list[RunGraphNode] = []
    for role in order:
        raw = members[role]
        status = statuses[role]

        deps: list[str] = []
        seen_dep: set[str] = set()
        for dep in _declared_deps(raw):
            if dep in members and dep not in seen_dep:
                deps.append(dep)
                seen_dep.add(dep)

        skip_reason: SkipReason | None = None
        reason_role: str | None = None
        if status == "skipped":
            record = member_skip_reasons_map.get(role)
            code = record.get("code") if isinstance(record, Mapping) else None
            recorded_role = record.get("role") if isinstance(record, Mapping) else None
            if (
                isinstance(record, Mapping)
                and code in _RECORDED_CODES
                and isinstance(recorded_role, str)
                and recorded_role
            ):
                skip_reason = cast("SkipReason", code)
                reason_role = recorded_role
            else:
                skip_reason = "unrecorded"
        elif status == "blocked":
            first_undelivered = next(
                (
                    dep
                    for dep in deps
                    if _dep_undelivered(
                        dep,
                        statuses=statuses,
                        outcome_faulted=outcome_faulted,
                        member_status=member_status_map,
                        results=results_map,
                    )
                ),
                None,
            )
            if first_undelivered is not None:
                skip_reason = "upstream_not_delivered"
                reason_role = first_undelivered
        elif status == "budget_skipped":
            skip_reason = "budget_exhausted"

        error_code: str | None = None
        if status == "failed":
            recorded_error = member_error_codes_map.get(role)
            if isinstance(recorded_error, str) and recorded_error:
                error_code = recorded_error

        input_from = [dep for dep in deps if results_map.get(dep) is not None]

        nodes.append(
            RunGraphNode(
                role=role,
                kind=kinds[role],
                status=status,
                error_code=error_code,
                skip_reason=skip_reason,
                reason_role=reason_role,
                input_from=input_from,
                has_output=results_map.get(role) is not None,
                loop=loop_of.get(role),
                fan_out=isinstance(raw.get("fan_out"), dict),
            )
        )

    # ── edges: declaration order over members, depends_on order over each member's deps ──
    edges: list[RunGraphEdge] = []
    seen_edges: set[tuple[str, str]] = set()
    for role in order:
        for dep in _declared_deps(members[role]):
            if dep not in members or dep == role:
                continue
            dep_loop = loop_of.get(dep)
            if dep_loop is not None and dep_loop == loop_of.get(role):
                continue
            key = (dep, role)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append(RunGraphEdge(from_=dep, to=role))

    return RunGraph(team_run_id=team_run_id, state=state, nodes=nodes, edges=edges)
