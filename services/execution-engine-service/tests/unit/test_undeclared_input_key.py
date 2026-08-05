"""#714 defect (b) — an ``inputs`` key the team does not consume is reported, not swallowed.

``resolve_run_task`` is explicit that only a manifest DECLARING ``task_input`` consumes it, and
``validate_task_input`` only fails closed when the key is declared *and* required. So a caller can
pass ``inputs: {"task": "Review pull request 710 …"}``, the engine discards it without a word, and
the model fills the hole with fiction — run ``538ab1fa`` sent GitHub ``my-org/my-repo#123``. The
user sees four ``MCP_TOOL_ERROR`` rows and no hint that their task was dropped. That is the
opposite of the fail-closed default (CLAUDE.md §3.5), and it is what turned a missing feature into
an undiagnosable MCP error.

``inputs`` is overloaded, so the check is against what the team ACTUALLY consumes, not against
``task_input`` alone. Three consumers exist and no others read team state:

- the declared ``task_input.key`` (``resolve_run_task``),
- any member's ``fan_out.over: "$.<key>"`` (#599, ``orchestrate._resolve_over``),
- the engine-reserved ``_refresh_seed`` (#602), which ``create`` strips just after.

RED-by-design until the ``[impl]`` lands: ``validate_input_keys`` does not exist yet, so it is
imported function-locally (§4.1) and these fail at runtime on the missing attribute.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_governance import Principal, PrincipalType
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_USER = uuid.uuid4()
_TASK = "Review pull request 710 in OraclousAI/oraclous-backend and post the review as a comment."


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _team(
    *,
    task_input: dict[str, Any] | None = None,
    fan_out_over: str | None = None,
) -> dict[str, Any]:
    member: dict[str, Any] = {
        "role": "worker",
        "kind": "agent",
        "manifest_ref": "org:x/worker@1",
        "subgoal": "do the work",
    }
    if fan_out_over is not None:
        member["fan_out"] = {"over": fan_out_over, "max_parallel": 2}
    doc: dict[str, Any] = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "team",
            "owner_organization_id": str(_ORG),
            "kind": "team",
        },
        "members": [member],
        "runtime": {"entrypoint": "worker"},
    }
    if task_input is not None:
        doc["task_input"] = task_input
    return doc


# ── the reported case ────────────────────────────────────────────────────────


def test_a_task_for_a_team_that_cannot_consume_one_is_a_422() -> None:
    """The exact shape of run ``538ab1fa``: the user supplied the task the only way the API offers,
    and the team had nowhere to put it."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_input_keys,
    )

    manifest = load_ohm(_team())
    with pytest.raises(TeamRunError) as err:
        validate_input_keys(manifest, {"task": _TASK})
    assert err.value.status_code == 422
    assert err.value.error_type == "undeclared_input_key"
    assert "task" in str(err.value)  # the message NAMES the key that would have been dropped


def test_the_message_names_every_undeclared_key() -> None:
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_input_keys,
    )

    manifest = load_ohm(_team())
    with pytest.raises(TeamRunError) as err:
        validate_input_keys(manifest, {"task": _TASK, "pr_url": "https://example.invalid/1"})
    message = str(err.value)
    assert "task" in message and "pr_url" in message


def test_a_wrong_key_on_a_declaring_team_is_still_reported() -> None:
    """The team declares ``pr_url`` and the caller sent ``task`` — a near miss that today runs to a
    confident wrong answer. ``validate_task_input`` cannot see it: the declared key is simply
    absent, and when it is optional that is legal."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        TeamRunError,
        validate_input_keys,
    )

    manifest = load_ohm(_team(task_input={"required": False, "key": "pr_url"}))
    with pytest.raises(TeamRunError) as err:
        validate_input_keys(manifest, {"task": _TASK})
    assert err.value.status_code == 422
    assert "pr_url" in str(err.value)  # and it says what the team DOES consume


# ── every legitimate use of `inputs` still passes ────────────────────────────


def test_the_declared_task_key_passes() -> None:
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        validate_input_keys,
    )

    manifest = load_ohm(_team(task_input={"required": True, "key": "task"}))
    validate_input_keys(manifest, {"task": _TASK})


def test_a_fan_out_key_passes() -> None:
    """#599: ``inputs`` is also the user-seeded team state a member fans out over. Rejecting it
    would break every running fan-out team, which is the one thing this check must not do."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        validate_input_keys,
    )

    manifest = load_ohm(_team(fan_out_over="$.items"))
    validate_input_keys(manifest, {"items": ["i1", "i2", "i3"]})


def test_a_fan_out_key_written_without_the_jsonpath_prefix_passes() -> None:
    # ``_resolve_over`` accepts a bare key as well as "$.<key>" — the check has to match it.
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        validate_input_keys,
    )

    manifest = load_ohm(_team(fan_out_over="items"))
    validate_input_keys(manifest, {"items": ["i1"]})


def test_the_reserved_refresh_seed_key_passes() -> None:
    """#602: ``_refresh_seed`` is engine-reserved and ``create`` strips it a few lines later. It
    must not 422 on the way past."""
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        validate_input_keys,
    )

    manifest = load_ohm(_team())
    validate_input_keys(manifest, {"_refresh_seed": {"records": []}})


@pytest.mark.parametrize("empty", [None, {}])
def test_no_inputs_passes(empty: dict[str, Any] | None) -> None:
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        validate_input_keys,
    )

    validate_input_keys(load_ohm(_team()), empty)


def test_a_team_consuming_both_a_task_and_a_fan_out_accepts_both() -> None:
    from oraclous_execution_engine_service.services.team_run_service import (  # §4.1 seam
        validate_input_keys,
    )

    manifest = load_ohm(_team(task_input={"required": True, "key": "task"}, fan_out_over="$.items"))
    validate_input_keys(manifest, {"task": _TASK, "items": ["a", "b"]})


# ── wired into create, before anything is persisted ──────────────────────────


class _RefusingRepo:
    """Any write is a failure — the 422 has to land before the run is persisted or enqueued."""

    async def create(self, **_kw: Any) -> Any:
        raise AssertionError("create_team_run persisted a run with an unconsumed inputs key")


async def test_create_rejects_before_persisting_or_enqueueing() -> None:
    from oraclous_execution_engine_service.services.team_run_service import (
        TeamRunError,
        TeamRunService,
    )

    enqueued: list[uuid.UUID] = []
    svc = TeamRunService(
        team_runs=_RefusingRepo(),  # type: ignore[arg-type] — duck-typed seam in unit tests
        harness=None,
        enqueue=lambda rid, _org, _user: enqueued.append(rid),
    )
    with pytest.raises(TeamRunError) as err:
        await svc.create(
            _principal(),
            manifest=_team(),
            sub_harnesses={},
            gate_decisions={},
            inputs={"task": _TASK},
        )
    assert err.value.status_code == 422
    assert err.value.error_type == "undeclared_input_key"
    assert enqueued == []  # no worker, no tokens
