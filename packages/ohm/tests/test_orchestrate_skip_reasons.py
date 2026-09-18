"""#1119 / #1154 — why a member was skipped, recorded when the run happens, not worked out later.

The new team-run graph read (`GET /v1/engine/team-runs/{id}/graph`, #1154) shows a `skip_reason` +
`reason_role` per node. Today ``_eval_run_if`` collapses three genuinely different situations into
one bare ``False`` — a condition that ran and failed, a condition whose source never produced
output, and a condition that could not even be evaluated (a ``TypeError``) — so nothing downstream
can tell them apart, and nothing records WHICH member's output a skipped member's condition tested.

These tests pin the split the graph read needs, entirely through the public ``run_team`` /
``run_team_hybrid`` surface:

* ``TeamRunResult`` gains ``member_skip_reasons: dict[str, dict[str, str]]`` — role -> {"code":
  one of "condition_false" | "condition_source_missing" | "condition_error", "role": the
  ``from_role`` the condition tested}. Only a ``run_if`` skip gets an entry; the existing injected
  ``predicate`` skip (the engine never uses it) gets none.
* A new best-effort hook, ``on_skip: Callable[[str, str, str], None]`` — ``(role, code,
  tested_role)`` — fired synchronously in the ``run_if`` skip branch, mirroring the existing
  ``on_checkpoint`` / ``on_dispatch`` / ``on_child`` posture (CLAUDE.md-visible ADR-042/#819/#828):
  a raising hook never aborts the run.
* The existing run/skip DECISION for every condition already expressible today must not change —
  only the recorded reason is new.

``run_team_hybrid`` (the driver that forwards ``on_checkpoint``/``on_dispatch`` to both the acyclic
skeleton and each loop) lives in ``execution-engine-service``, a different service/layer than this
package — it is exercised in that service's own tests. What IS testable here, at the
``oraclous_ohm`` layer, is the actual seam the hybrid driver uses to run its skeleton:
``run_team(..., members=<the acyclic subset>)`` (see ``run_team``'s own docstring). The last test
below drives exactly that path.

RED until ``member_skip_reasons`` and ``on_skip`` land on ``TeamRunResult`` / ``run_team``. Both
symbols under test already exist and are safe to import at module level; the missing pieces are a
field and a keyword, so failures surface as ``AttributeError`` / ``TypeError`` at runtime, not at
collection.
"""

from __future__ import annotations

import operator
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import (
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMRunIf,
    OHMRuntime,
)
from oraclous_ohm.orchestrate import run_team

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(role: str, depends_on: list[str] | None = None) -> OHMMember:
    return OHMMember(
        role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=depends_on or []
    )


def _cond(role: str, deps: list[str], run_if: OHMRunIf) -> OHMMember:
    return OHMMember(
        role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=deps, run_if=run_if
    )


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


# ── (1) condition_false, including the field-absent-from-a-dict-source sub-case ─────────────────


@pytest.mark.parametrize(
    "produced",
    [
        pytest.param({"x": 2}, id="field-present-but-wrong"),
        pytest.param({"y": 2}, id="field-absent-reads-as-None-not-source-missing"),
    ],
)
async def test_run_if_false_records_condition_false_and_tested_role(
    produced: dict[str, Any],
) -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return produced if member.role == "research" else {"ok": member.role}

    target = _cond(
        "target", ["research"], OHMRunIf(from_role="research", field="x", op="eq", value=1)
    )
    res = await run_team(_team([_m("research"), target]), dispatch)

    assert "target" in res.skipped
    assert res.member_skip_reasons["target"] == {"code": "condition_false", "role": "research"}


# ── (2) condition_source_missing: the tested member never produced output ───────────────────────


async def test_run_if_on_a_skipped_source_records_condition_source_missing() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"regime": "flat"} if member.role == "flag" else {"ok": member.role}

    upstream = _cond(
        "upstream", ["flag"], OHMRunIf(from_role="flag", field="regime", op="eq", value="tradeable")
    )
    target = _cond("target", ["upstream"], OHMRunIf(from_role="upstream", op="truthy"))
    res = await run_team(_team([_m("flag"), upstream, target]), dispatch)

    assert "upstream" in res.skipped  # sanity: the chain skips as it did before this change
    assert "target" in res.skipped
    assert res.member_skip_reasons["target"] == {
        "code": "condition_source_missing",
        "role": "upstream",
    }


# ── (3) condition_error: a TypeError during comparison ───────────────────────────────────────────


async def test_run_if_type_error_records_condition_error() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"letter": "x"} if member.role == "num" else {"ok": member.role}

    # "in" against a non-container `value` (an int) raises TypeError inside _eval_run_if's match.
    target = _cond("target", ["num"], OHMRunIf(from_role="num", field="letter", op="in", value=5))
    res = await run_team(_team([_m("num"), target]), dispatch)

    assert "target" in res.skipped
    assert res.member_skip_reasons["target"] == {"code": "condition_error", "role": "num"}


# ── (4) non-regression: every run/skip DECISION expressible today is unchanged ───────────────────

_CMP_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "lt": operator.lt,
    "gte": operator.ge,
    "lte": operator.le,
}


def _expected_run(op: str, field_value: Any, cond_value: Any) -> bool:
    """The CURRENT, documented fail-closed semantics of ``_eval_run_if`` — computed independently
    of the implementation (never hand-derived) so this pins the decision, not a guessed literal."""
    if op == "truthy":
        return bool(field_value)
    if op == "in":
        return bool(field_value in cond_value)
    return _CMP_OPS[op](field_value, cond_value)


@pytest.mark.parametrize(
    "op,field_value,cond_value",
    [
        ("eq", 1, 1),
        ("eq", 2, 1),
        ("ne", 1, 2),
        ("ne", 1, 1),
        ("gt", 5, 3),
        ("gt", 2, 3),
        ("lt", 1, 3),
        ("lt", 5, 3),
        ("gte", 3, 3),
        ("gte", 2, 3),
        ("lte", 3, 3),
        ("lte", 5, 3),
        ("truthy", "yes", None),
        ("truthy", "", None),
        ("truthy", 0, None),
        ("in", "b", ["a", "b", "c"]),
        ("in", "z", ["a", "b", "c"]),
    ],
)
async def test_skip_reasons_leave_every_run_or_skip_decision_unchanged(
    op: str, field_value: Any, cond_value: Any
) -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"x": field_value} if member.role == "research" else {"ok": member.role}

    target = _cond(
        "target", ["research"], OHMRunIf(from_role="research", field="x", op=op, value=cond_value)
    )
    res = await run_team(_team([_m("research"), target]), dispatch)

    expected_run = _expected_run(op, field_value, cond_value)
    if expected_run:
        assert "target" not in res.skipped
        assert res.results["target"] == {"ok": "target"}
        assert res.member_status["target"] == "succeeded"
        assert "target" not in res.member_skip_reasons
    else:
        assert "target" in res.skipped
        assert res.results["target"] is None
        assert res.member_status["target"] == "skipped"
        # every one of these combinations is a condition that RAN and read false — never a missing
        # source or a type error — so the new reason code must not shift the existing decision.
        assert res.member_skip_reasons["target"] == {"code": "condition_false", "role": "research"}


# ── (5) only a run_if-skipped member carries a reason ────────────────────────────────────────────


async def test_only_skipped_members_carry_a_skip_reason() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"x": 2} if member.role == "research" else {"ok": member.role}

    blocked_by_cond = _cond(
        "gated", ["research"], OHMRunIf(from_role="research", field="x", op="eq", value=1)
    )
    runs_fine = _m("fine", ["research"])
    res = await run_team(_team([_m("research"), blocked_by_cond, runs_fine]), dispatch)

    assert set(res.skipped) == {"gated"}
    assert set(res.member_skip_reasons.keys()) == {"gated"}


# ── (6) on_skip fires before the member's own checkpoint ─────────────────────────────────────────


class _OrderLog:
    """Tags each hook firing in call order, so ordering between two DIFFERENT hooks can be asserted
    without depending on wall-clock timing. A checkpoint is recorded once per role the FIRST time
    that role shows up "skipped" in a snapshot (later snapshots repeat the same fact)."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def on_skip(self, role: str, code: str, tested_role: str) -> None:
        self.events.append(f"skip:{role}")

    async def on_checkpoint(self, results: dict[str, Any], member_status: dict[str, str]) -> None:
        for role, status in member_status.items():
            tag = f"checkpoint_skipped:{role}"
            if status == "skipped" and tag not in self.events:
                self.events.append(tag)


async def test_on_skip_fires_before_the_members_checkpoint() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"x": 2} if member.role == "research" else {"ok": member.role}

    target = _cond(
        "target", ["research"], OHMRunIf(from_role="research", field="x", op="eq", value=1)
    )
    log = _OrderLog()
    await run_team(
        _team([_m("research"), target]),
        dispatch,
        on_skip=log.on_skip,
        on_checkpoint=log.on_checkpoint,
    )

    assert "skip:target" in log.events
    assert "checkpoint_skipped:target" in log.events
    assert log.events.index("skip:target") < log.events.index("checkpoint_skipped:target")


# ── (7) a raising on_skip is best-effort — it never aborts the run ───────────────────────────────


async def test_a_raising_on_skip_never_aborts_the_run() -> None:
    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"x": 2} if member.role == "research" else {"ok": member.role}

    def exploding(role: str, code: str, tested_role: str) -> None:
        raise RuntimeError("the durability sink went away")

    target = _cond(
        "target", ["research"], OHMRunIf(from_role="research", field="x", op="eq", value=1)
    )
    other = _m("independent")  # an unrelated stage-mate that must still run to completion
    res = await run_team(_team([_m("research"), target, other]), dispatch, on_skip=exploding)

    assert res.status == "completed"
    assert res.member_status["target"] == "skipped"
    assert res.member_status["independent"] == "succeeded"


# ── (8) an injected predicate skip records NO reason (the engine never passes one) ───────────────


async def test_predicate_skip_records_no_reason() -> None:
    calls: list[tuple[str, str, str]] = []

    def on_skip(role: str, code: str, tested_role: str) -> None:
        calls.append((role, code, tested_role))

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"ok": member.role}

    def predicate(member: OHMMember, results: dict[str, Any]) -> bool:
        return member.role != "b"

    res = await run_team(
        _team([_m("a"), _m("b", ["a"])]), dispatch, predicate=predicate, on_skip=on_skip
    )

    assert "b" in res.skipped
    assert res.member_status["b"] == "skipped"
    assert "b" not in res.member_skip_reasons
    assert calls == []  # on_skip is a run_if-only signal; a predicate skip never fires it


# ── (9) the hybrid skeleton seam (run_team's own `members=` override) forwards on_skip too ───────


async def test_run_teams_skeleton_seam_threads_on_skip_too() -> None:
    # run_team's docstring: "the hybrid driver (ADR-043 #552) passes the acyclic skeleton + one
    # condensed node per loop, so run_team schedules the loops at their topological position" via
    # the `members=` override. `on_skip` must be forwarded through that exact call the same way
    # `on_checkpoint` already is, so a run_if-skipped member INSIDE the reduced skeleton still gets
    # recorded even though a loop role riding along in `manifest.members` is excluded from it.
    calls: list[tuple[str, str, str]] = []

    def on_skip(role: str, code: str, tested_role: str) -> None:
        calls.append((role, code, tested_role))

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        return {"regime": "flat"} if member.role == "skeleton_a" else {"ok": member.role}

    skeleton_a = _m("skeleton_a")
    skeleton_b = _cond(
        "skeleton_b",
        ["skeleton_a"],
        OHMRunIf(from_role="skeleton_a", field="regime", op="eq", value="tradeable"),
    )
    # stands in for a loop's condensed node — the real hybrid driver runs it separately through
    # run_loop_seam and never includes it in the skeleton's `members=` list passed to run_team.
    loop_member = _m("loop_member")
    manifest = _team([skeleton_a, skeleton_b, loop_member])

    res = await run_team(manifest, dispatch, members=[skeleton_a, skeleton_b], on_skip=on_skip)

    assert calls == [("skeleton_b", "condition_false", "skeleton_a")]
    assert res.member_skip_reasons == {
        "skeleton_b": {"code": "condition_false", "role": "skeleton_a"}
    }
    assert "loop_member" not in res.member_status  # excluded from this skeleton-only DAG call
