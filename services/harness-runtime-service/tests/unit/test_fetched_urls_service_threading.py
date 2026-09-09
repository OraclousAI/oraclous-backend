"""#975 (§CITE cite-by-reference), plan §4 — `HarnessExecutionService` threads the fetch-registry
seeds into the loop and persists what comes back, on every call site.

Mirrors the fake-registry harness `test_memory_hook.py` uses to drive a real `execute()` through
the fake LLM (`_manifest`/`_FakeRegistry`/`llm_mode="fake"`), plus `test_resume_service.py`'s fake
execution/checkpoint repos for the resume path. `run_tool_use_loop` itself is monkeypatched to a
capturing stub — its own behaviour (registering seeds, computing `LoopResult.fetched_urls`) is
T2's loop-level coverage, not this file's; this file proves the SERVICE passes the right values in
and persists the right values out, regardless of what the loop does with them.

RED today: `HarnessExecutionService.execute()`/`resume()` accept neither `prior_fetched_urls` nor
`person_supplied_text`, so the explicit-value tests fail with `TypeError`; the default/persist tests
call the loop and repos exactly as they do today, so they fail on a missing key (the service never
passes or persists a value it doesn't yet know about) — a behaviour assertion, not just a TypeError.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_governance import Principal, PrincipalType
from oraclous_ohm.signatures import TrustStore

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()
_USER = uuid.uuid4()

_A = "https://example.com/a"
_B = "https://example.com/b"

_DESCRIPTOR = {
    "id": "cap-1",
    "metadata": {"name": "Echo"},
    "spec": {"capabilities": [{"name": "run", "description": "Echo back", "parameters": {}}]},
}


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _manifest() -> dict[str, Any]:
    return {
        "ohm_version": "1.0",
        "metadata": {
            "id": "01976e3a-7c9b-7b00-9c45-1234567890ab",
            "name": "Fetch Registry Demo",
            "owner_organization_id": str(_ORG),
        },
        "capabilities": [{"ref": "core/echo@1.0.0", "binding": "echo"}],
        "prompts": [{"role": "primary", "source": "inline", "body": "You are helpful."}],
        "runtime": {"entrypoint": "echo"},
    }


class _FakeRegistry:
    async def resolve_capability(self, ref: str, *, explicit_id: str | None = None) -> dict:
        return {"id": "cap-1", "name": "Echo", "descriptor": _DESCRIPTOR}

    async def list_instances(self) -> list[dict]:
        return []

    async def create_instance(self, *, capability_id: str, name: str, configuration: dict) -> dict:
        return {"id": str(uuid.uuid4())}

    async def configure_credentials(self, instance_id: uuid.UUID, mappings: dict) -> dict:
        return {}

    async def execute(self, instance_id: uuid.UUID, input_data: dict) -> dict:
        return {"status": "SUCCESS", "output_data": {}}


class _FakeExecutions:
    def __init__(self, row: Any = None) -> None:
        self._row = row
        self.created: dict[str, Any] | None = None
        self.updated: dict[str, Any] | None = None

    async def create(self, **fields: Any) -> SimpleNamespace:
        self.created = fields
        return SimpleNamespace(id=fields["execution_id"], **fields)

    async def get(self, execution_id: Any, organisation_id: Any) -> Any:
        return self._row if self._row and self._row.organisation_id == organisation_id else None

    async def update_run(self, execution_id: Any, organisation_id: Any, **fields: Any) -> Any:
        self.updated = fields
        return SimpleNamespace(id=execution_id, **fields)


class _FakeCheckpoints:
    def __init__(self, cursor: dict[str, Any] | None = None, pending: bool = True) -> None:
        self._pending = pending
        self._cursor = cursor or {"iteration": 1, "tool_calls_made": 0, "tokens_used": 0}

    async def get_latest_pending(self, execution_id: Any, organisation_id: Any) -> Any:
        if not self._pending:
            return None
        return SimpleNamespace(
            id=uuid.uuid4(),
            manifest_doc=_manifest(),
            resume_messages=[],
            pending_tool_calls=[],
            approved_tool_call_id=None,
            redact_patterns=[],
            resume_cursor=self._cursor,
        )

    async def set_decision(self, checkpoint_id: Any, organisation_id: Any, new_status: str) -> Any:
        return SimpleNamespace(id=checkpoint_id, status=new_status)

    async def revert_to_pending(self, checkpoint_id: Any, organisation_id: Any) -> None:
        pass

    async def create(self, **fields: Any) -> SimpleNamespace:
        return SimpleNamespace(id=uuid.uuid4())


class _FakeProv:
    async def emit(self, record: Any) -> None:
        pass


def _service(executions: Any, checkpoints: Any = None) -> Any:
    from oraclous_harness_runtime_service.services.harness_execution_service import (
        HarnessExecutionService,
    )

    return HarnessExecutionService(
        registry=_FakeRegistry(),
        broker=None,
        executions=executions,
        assignments=None,
        checkpoints=checkpoints,
        provenance=_FakeProv(),
        trust=TrustStore({}),
        require_signature=False,
        force_policy_set=None,
        llm_mode="fake",
        llm_base_urls={},
        llm_timeout=1.0,
        llm_allow_private=True,
        max_iterations=6,
    )


def _stub_loop(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict[str, Any],
    *,
    fetched_urls: list[str] | None = None,
    status: str = "SUCCEEDED",
    error_type: str | None = None,
    error_message: str | None = None,
    checkpoint: Any = None,
) -> None:
    from oraclous_harness_runtime_service.models.enums import HarnessStatus

    async def fake_run_tool_use_loop(**kwargs: Any) -> SimpleNamespace:
        captured.clear()
        captured.update(kwargs)
        return SimpleNamespace(
            status=HarnessStatus(status),
            error_type=error_type,
            error_message=error_message,
            checkpoint=checkpoint,
            output="done",
            steps=[],
            total_tokens=0,
            iterations=1,
            input_tokens=0,
            output_tokens=0,
            protocol_shape="fake",
            served_citation_ids=[],
            fetched_urls=list(fetched_urls or []),
        )

    import oraclous_harness_runtime_service.services.harness_execution_service as svc_mod

    monkeypatch.setattr(svc_mod, "run_tool_use_loop", fake_run_tool_use_loop)


# --- execute(): explicit values thread straight into the loop call ------------------------------


async def test_execute_threads_explicit_seeds_into_the_loop_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _stub_loop(monkeypatch, captured)
    svc = _service(_FakeExecutions())
    await svc.execute(
        manifest_inline=_manifest(),
        manifest_ref=None,
        user_input="go",
        principal=_principal(),
        prior_fetched_urls=[_A],
        person_supplied_text="the founder's own text",
    )  # TypeError today: execute() accepts neither kwarg
    assert captured["prior_fetched_urls"] == [_A]
    assert captured["person_supplied_text"] == "the founder's own text"


# --- A8: the standalone default is the run's own user_input -------------------------------------


async def test_execute_defaults_person_supplied_text_to_user_input_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _stub_loop(monkeypatch, captured)
    svc = _service(_FakeExecutions())
    await svc.execute(
        manifest_inline=_manifest(),
        manifest_ref=None,
        user_input="go fetch it",
        principal=_principal(),
    )
    # today: the service never passes person_supplied_text at all — KeyError, not a TypeError.
    assert captured["person_supplied_text"] == "go fetch it"


# --- persistence: result.fetched_urls is written on both the success and the escalate/pause path
# (A3) — both go through the SAME `create()` call in execute(), so one call site, two statuses.


async def test_execute_persists_fetched_urls_on_the_success_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _stub_loop(monkeypatch, captured, fetched_urls=[_A, _B])
    execs = _FakeExecutions()
    svc = _service(execs)
    await svc.execute(
        manifest_inline=_manifest(), manifest_ref=None, user_input="go", principal=_principal()
    )
    assert execs.created is not None
    assert execs.created["fetched_urls"] == [_A, _B]  # today: KeyError — never passed to create()


async def test_execute_persists_fetched_urls_on_the_escalate_pause_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from oraclous_harness_runtime_service.domain.loop.tool_use import LoopCheckpoint

    checkpoint = LoopCheckpoint(
        messages=[],
        pending_tool_calls=[],
        approved_tool_call_id="c1",
        iteration=1,
        tool_calls_made=1,
        tokens_used=10,
        redact_patterns=[],
    )
    captured: dict[str, Any] = {}
    _stub_loop(
        monkeypatch,
        captured,
        fetched_urls=[_A],
        status="ESCALATED",
        error_type="hitl_required",
        checkpoint=checkpoint,
    )
    execs = _FakeExecutions()
    svc = _service(execs, checkpoints=_FakeCheckpoints())
    await svc.execute(
        manifest_inline=_manifest(), manifest_ref=None, user_input="go", principal=_principal()
    )
    assert execs.created is not None
    assert execs.created["status"] == "ESCALATED"
    assert execs.created["fetched_urls"] == [_A]  # today: KeyError — never passed to create()


# --- resume: A3 — prior_fetched_urls is the PERSISTED union, not a fresh loop-side list ----------


def _paused_execution(fetched_urls: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=_ORG,
        status="ESCALATED",
        error_type="hitl_required",
        output="partial",
        iterations=2,
        total_tokens=10,
        input_tokens=0,
        output_tokens=0,
        steps=[{"index": 0, "kind": "gate", "name": "x", "status": "hitl_required", "detail": "d"}],
        input="go",
        harness_name="Demo",
        served_citation_ids=[],
        fetched_urls=fetched_urls,
    )


async def test_resume_approved_passes_the_persisted_union_as_prior_fetched_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _paused_execution([_A, _B])
    captured: dict[str, Any] = {}
    _stub_loop(monkeypatch, captured)
    execs = _FakeExecutions(row)
    svc = _service(execs, checkpoints=_FakeCheckpoints())
    await svc.resume(execution_id=row.id, principal=_principal(), decision="APPROVED")
    # today: resume() never reads execution.fetched_urls or passes prior_fetched_urls — KeyError.
    assert captured["prior_fetched_urls"] == [_A, _B]


async def test_resume_approved_persists_the_new_result_fetched_urls_via_update_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _paused_execution([_A])
    captured: dict[str, Any] = {}
    _stub_loop(monkeypatch, captured, fetched_urls=[_B])
    execs = _FakeExecutions(row)
    svc = _service(execs, checkpoints=_FakeCheckpoints())
    await svc.resume(execution_id=row.id, principal=_principal(), decision="APPROVED")
    assert execs.updated is not None
    # today: KeyError — update_run is never called with fetched_urls on the resume path.
    assert execs.updated["fetched_urls"] == [_B]


# --- S7: person_supplied_text is never logged ----------------------------------------------------


async def test_person_supplied_text_is_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel = "SENTINEL-do-not-log-df0c3b2a"
    captured: dict[str, Any] = {}
    _stub_loop(monkeypatch, captured)
    execs = _FakeExecutions()
    svc = _service(execs)
    with caplog.at_level(logging.DEBUG):
        row = await svc.execute(
            manifest_inline=_manifest(),
            manifest_ref=None,
            user_input="go",
            principal=_principal(),
            person_supplied_text=sentinel,
        )  # TypeError today: execute() accepts no such kwarg
    assert sentinel not in caplog.text
    assert sentinel not in (row.output or "")
    assert all(sentinel not in str(step) for step in row.steps)


async def test_person_supplied_text_is_never_logged_or_surfaced_on_a_failed_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # MINOR 1 (be-test-reviewer, PR #976): the test above only proves S7 on the SUCCEEDED path.
    # Drive a FAILED run the same way the ESCALATED-path neighbour above
    # (`test_execute_persists_fetched_urls_on_the_escalate_pause_path`) drives its status —
    # `_stub_loop` with a non-SUCCEEDED status — and prove the sentinel is absent everywhere a
    # failed run narrates itself: the caplog record, the persisted `error_message`, and every
    # step's detail.
    sentinel = "SENTINEL-do-not-leak-on-failure-a91cf76e"
    captured: dict[str, Any] = {}
    _stub_loop(
        monkeypatch,
        captured,
        status="FAILED",
        error_type="model_unavailable",
        error_message="the model endpoint is unreachable",
    )
    execs = _FakeExecutions()
    svc = _service(execs)
    with caplog.at_level(logging.DEBUG):
        row = await svc.execute(
            manifest_inline=_manifest(),
            manifest_ref=None,
            user_input="go",
            principal=_principal(),
            person_supplied_text=sentinel,
        )  # TypeError today: execute() accepts no such kwarg
    assert row.status == "FAILED"
    assert sentinel not in caplog.text
    assert sentinel not in (row.error_message or "")
    assert all(sentinel not in str(step) for step in row.steps)
