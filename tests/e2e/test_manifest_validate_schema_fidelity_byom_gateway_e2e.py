"""#911 nightly real-model regression guard: the reviewer's ``manifest-validate`` tool call must
succeed against a REAL model, not just at the unit-schema level.

Background: the first-party ``core/manifest-validate@1`` capability's model-facing tool schema was
dropping ``required``, so a model calling ``validate_manifest`` was never told ``draft`` is
required — the registry then rejected the call with
``{"error": "RegistryError", "detail": "tool execution failed: draft is required"}``. The two unit
commits already on this branch (``ac6b7fd2``, ``94d203e9``) pin the fix at the schema-projection and
config-threading level. Neither proves a REAL model actually sends the argument correctly once the
schema is fixed — that needs a live tool call, so this is the permanent BYOM regression guard.

WHY INDIRECT: no gateway route exposes the arguments a model actually sent for a tool call — the
step trace (``StepOut``: ``index``/``kind``/``name``/``status``/``detail``/``tool_call_id``, see
``services/harness-runtime-service/.../schema/harness_schemas.py``) never carries the call's raw
input, and the registry's execution read-back omits ``input_data`` too. So this drives a real
compiler run (planner -> manifest-drafter -> reviewer) through the gateway and reads the run back
via ``GET /v1/engine/team-runs/{id}``, taking ``results["reviewer"]["steps"]`` — a list of dicts in
the ``StepOut`` shape — filtered to the tool step named ``"manifest-validate.validate_manifest"``
(``step_name = f"{spec.binding}.{spec.operation}"``,
``services/harness-runtime-service/.../domain/loop/tool_use.py``). The FIRST such step's
``status`` is the load-bearing assertion: ``"ok"`` proves the model's real arguments satisfied the
schema; today's bug reproduces as ``"error"`` with ``detail`` naming the missing ``draft`` field.

CONFIRMED WITHOUT RUNNING ANYTHING (grepped, not guessed): the compiler's reviewer role has
``manifest-validate`` baked into its toolset unconditionally by
``packages/ohm/src/oraclous_ohm/compiler/team.py``'s ``build_compiler_team`` —
``OHMMember(role="reviewer", ..., tools=[_VALIDATE_TOOL])`` where ``_VALIDATE_TOOL =
"manifest-validate"`` (team.py:34, :171) — and its sub-harness is built with the same tool
(team.py:209). The reviewer's system prompt (``packages/ohm/src/oraclous_ohm/compiler/prompts.py``,
``REVIEWER_PROMPT``) explicitly instructs it: "Call `manifest-validate` ONCE on the drafted team
JSON" (prompts.py:102), refusing to emit a team until ``would_block`` is false. So NEITHER the
toolset NOR the instruction to call it depends on this test's objective wording — an ordinary
compiler objective is enough to reach the tool step. (What is NOT guaranteed without running the
stack: a live model may occasionally skip the call, degrade, or the run may fail later for the
UNRELATED #1014 reviewer-JSON-parsing bug — see the constraints below.)

TWO HARD CONSTRAINTS on how this stays a valid guard once #1014 lands:

1. The compiler's real-model path has its OWN separate known bug (#1014): a greedy JSON-peel in the
   reviewer's response parsing breaks on a real model's trailing block, AFTER the
   ``manifest-validate`` tool step this test cares about has already run. This is exactly why the
   compiler surface (``test_compiler_prose_to_team_gateway_e2e.py``) is excluded from the per-PR
   ``byom_smoke`` subset (``tests/e2e/README.md``). So this test asserts ONLY on the
   ``manifest-validate.validate_manifest`` tool step's ``status`` — never on the run's overall
   terminal ``state`` being ``SUCCEEDED``, since a #1014 failure can legitimately fail the run AFTER
   a genuinely-``"ok"`` tool step.
2. Skip-free: if the run never produced a ``manifest-validate.validate_manifest`` step at all (the
   model never called the tool, wrong name, etc.), this test FAILS with a clear message — it never
   calls ``pytest.skip`` for that condition. The only skip in this file is the module-level
   ``requires_byom`` guard for a completely different condition (no ``OPENROUTER_API_KEY``
   configured in this environment at all).
3. Model non-determinism: only the FIRST such step's status is asserted. No assertion on an exact
   call count, and no assertion that the tool was called more than once or exactly once (the
   reviewer's in-harness repair loop may call it again to re-validate a fix).

Markers: ``byom`` only (the full nightly real-model leg) — deliberately NOT ``byom_smoke``, since
this rides the same #1014-affected compiler surface the README says to keep out of the per-PR
subset until that parsing bug is fixed.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_OR_KEY = os.environ.get("OPENROUTER_API_KEY")
requires_byom = pytest.mark.skipif(_OR_KEY is None, reason="OPENROUTER_API_KEY unset (real BYOM)")
_MODEL = os.environ["E2E_MODEL"]

#: an ordinary compiler objective — no special wording is needed to reach the tool step (see the
#: module docstring: the reviewer's toolset and its instruction to validate are both baked in,
#: independent of the objective's phrasing).
_OBJECTIVE = "Research this week's most-cited AI papers and compile a short plain-text digest."

#: the exact step name `step_name = f"{spec.binding}.{spec.operation}"` produces for the reviewer's
#: validate call (tool_use.py:1615/1654; binding="manifest-validate", operation="validate_manifest"
#: per builtin.py's ManifestValidateConnector).
_VALIDATE_STEP_NAME = "manifest-validate.validate_manifest"


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


def _poll(c: httpx.Client, run_id: str, tries: int = 160) -> dict:
    row: dict = {}
    for _ in range(tries):
        row = c.get(f"/v1/engine/team-runs/{run_id}").json()
        if row["state"] in {"SUCCEEDED", "FAILED", "REJECTED", "PAUSED"}:
            return row
        time.sleep(3)
    raise AssertionError(f"run {run_id} never terminated (last: {row.get('state')})")


@requires_byom
@pytest.mark.byom
def test_reviewer_manifest_validate_call_succeeds_with_a_real_model(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """#911 regression guard: the FIRST ``manifest-validate.validate_manifest`` tool step the real
    reviewer produces must be ``"ok"`` — proving a real model's arguments satisfy the (fixed)
    model-facing schema, including the now-advertised ``required: ["draft"]``.

    Deliberately does NOT assert the run's overall terminal ``state`` (constraint 1: the compiler's
    real-model path has the separate, already-known #1014 parsing bug that can fail the run AFTER
    this tool step has already run genuinely ok) and deliberately does NOT skip when the step is
    missing (constraint 2: a missing step is a hard failure of this guard, not an environment
    condition)."""
    user = register(f"schema{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    cred = _cred(c, user)
    gid = c.post("/api/v1/graphs", json={"name": "manifest-validate-schema-fidelity"}).json()["id"]

    compiled = c.post(
        "/v1/engine/compiler-runs",
        json={"objective": _OBJECTIVE, "models": [_model(cred)], "graph_id": gid},
    )
    assert compiled.status_code == 202, compiled.text
    run = _poll(c, compiled.json()["id"])

    reviewer = run.get("results", {}).get("reviewer")
    assert isinstance(reviewer, dict) and isinstance(reviewer.get("steps"), list), (
        f"the compiler run produced no readable reviewer step trace "
        f"(run state={run.get('state')!r}); results keys={sorted(run.get('results', {}))}: {run}"
    )
    steps = reviewer["steps"]

    validate_steps = [
        s for s in steps if s.get("kind") == "tool" and s.get("name") == _VALIDATE_STEP_NAME
    ]
    assert validate_steps, (
        f"the reviewer never produced a {_VALIDATE_STEP_NAME!r} tool step (run state="
        f"{run.get('state')!r}); this is a hard failure of the #911 guard, not a skip — reviewer "
        f"steps were: {steps}"
    )

    first = validate_steps[0]
    assert first.get("status") == "ok", (
        f"the reviewer's FIRST {_VALIDATE_STEP_NAME!r} call must succeed against a real model's "
        f"arguments (run state={run.get('state')!r} — #1014's later JSON-peel bug is a separate, "
        f"already-known issue and is NOT what this test targets); "
        f"step detail: {first.get('detail')!r}"
    )
