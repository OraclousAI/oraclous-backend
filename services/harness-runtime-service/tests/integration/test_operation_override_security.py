"""Security (#956): a model that injects ``operation`` into its tool-call arguments cannot choose
which operation the tool runs — driven end to end through ``HarnessExecutionService.execute()``.

Threat (capability confusion): binding a tool to an operation is how the runtime decides what a
member may do. A model handed ``gh__read_file`` writes ``{"operation": "delete_repo", ...}`` in
its function-call arguments; today ``{"operation": spec.operation, **args}`` lets the second key
win and the connector runs whatever the model named. A member granted one operation reaches the
connector's others.

What is real here: the service's own ``execute()`` (manifest load, policy, ``_build_runnable``,
the real dispatch closure), the real ``run_tool_use_loop``, the real step trace and persistence
calls. What is scripted: the LLM (a client that sends the attack, reads the refusal, adapts, and
answers) and the registry (a recorder standing in for the HTTP client — this is the boundary the
threat crosses, so it is the boundary observed). The repositories are the in-memory fakes
``test_fetched_urls_service_threading.py`` uses to drive ``execute()``.

Why not through the gateway: a scripted LLM cannot be brought through the deployed stack's public
API — ``HARNESS_LLM_MODE=fake`` is the service's own fixed script and never emits an ``operation``
key, and a real model cannot be made to emit one on cue. So the live-stack leg for this issue
shows normal tool calling still works after the strip; the attack itself is proven here.

Pinned (#956 ruling 1):

* the registry never receives the injected operation — the ONLY payload it sees is the bound one;
* the refusal is fed back to the model as a coded tool error, and the run goes on (the model gets
  its turn to adapt — the #946 T2 ruling for a refused call);
* the persisted step trace records the refusal as an ``error`` tool step carrying the code;
* an injected ``operation`` equal to the bound one is stripped and the call proceeds once.

RED until the #956 ``[impl]`` lands.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from oraclous_governance import Principal, PrincipalType
from oraclous_harness_runtime_service.domain.llm.base import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
)
from oraclous_harness_runtime_service.models.enums import HarnessStatus, StepKind
from oraclous_ohm.signatures import TrustStore

pytestmark = [pytest.mark.integration, pytest.mark.security, pytest.mark.tool_dispatch]

_ORG = uuid.uuid4()
_USER = uuid.uuid4()
_CODE = "operation_override_refused"
_TOOL = "gh__read_file"

_DESCRIPTOR = {
    "id": "cap-gh",
    "kind": "tool",
    "metadata": {"name": "GitHub Reader"},
    "spec": {
        "type": "API",
        "capabilities": [
            {"name": "read_file", "description": "Read a file", "parameters": {"repo": "str"}},
        ],
    },
}


def _principal() -> Principal:
    return Principal(principal_id=_USER, principal_type=PrincipalType.USER, organisation_id=_ORG)


def _manifest() -> dict[str, Any]:
    return {
        "ohm_version": "1.0",
        "metadata": {
            "id": "01976e3a-7c9b-7b00-9c45-000000000956",
            "name": "Operation Override Probe",
            "owner_organization_id": str(_ORG),
        },
        "capabilities": [{"ref": "core/github-reader@1.0.0", "binding": "gh"}],
        "prompts": [{"role": "primary", "source": "inline", "body": "Read the file."}],
        "runtime": {"entrypoint": "gh"},
    }


class _Registry:
    def __init__(self) -> None:
        self.executed: list[dict[str, Any]] = []

    async def resolve_capability(self, ref: str, *, explicit_id: str | None = None) -> dict:
        return {"id": "cap-gh", "name": "GitHub Reader", "descriptor": _DESCRIPTOR}

    async def list_instances(self) -> list[dict]:
        return []

    async def create_instance(self, *, capability_id: str, name: str, configuration: dict) -> dict:
        return {"id": str(uuid.uuid4())}

    async def configure_credentials(self, instance_id: uuid.UUID, mappings: dict) -> dict:
        return {}

    async def execute(self, instance_id: uuid.UUID, input_data: dict) -> dict:
        self.executed.append(dict(input_data))
        return {"status": "SUCCESS", "output_data": {"content": "# README"}}


class _Executions:
    def __init__(self) -> None:
        self.created: dict[str, Any] | None = None

    async def create(self, **fields: Any) -> SimpleNamespace:
        self.created = fields
        return SimpleNamespace(id=fields["execution_id"], **fields)


class _Provenance:
    async def emit(self, record: Any) -> None:
        pass


class _AttackingLLM:
    """Turn 1: the attack. Turn 2: whatever came back, retry without the key. Turn 3: answer.

    Records every transcript it is handed so the test can assert what the MEMBER read — the tool-
    role reply is the channel the ruling says the refusal must travel on.
    """

    protocol_shape = "fake"

    def __init__(self, first_args: dict[str, Any]) -> None:
        self.first_args = first_args
        self.turns = 0
        self.transcripts: list[list[Message]] = []

    async def complete(
        self, *, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> LLMResponse:
        self.turns += 1
        self.transcripts.append([dict(m) for m in messages])
        if self.turns == 1:
            return LLMResponse(text="", tool_calls=[ToolCall("c1", _TOOL, dict(self.first_args))])
        if self.turns == 2:
            return LLMResponse(text="", tool_calls=[ToolCall("c2", _TOOL, {"repo": "a/b"})])
        return LLMResponse(text="The README says hello.")


def _service(registry: _Registry, executions: _Executions, llm: _AttackingLLM) -> Any:
    from oraclous_harness_runtime_service.services.harness_execution_service import (
        HarnessExecutionService,
    )

    service = HarnessExecutionService(
        registry=registry,
        broker=None,
        executions=executions,
        assignments=None,
        checkpoints=None,
        provenance=_Provenance(),
        trust=TrustStore({}),
        require_signature=False,
        force_policy_set=None,
        llm_mode="fake",
        llm_base_urls={},
        llm_timeout=1.0,
        llm_allow_private=True,
        max_iterations=6,
    )

    async def _build_llm(_manifest: Any, _org_id: Any) -> Any:  # noqa: ANN401
        return llm

    service._build_llm = _build_llm  # type: ignore[method-assign]
    return service


def _tool_messages(transcript: list[Message]) -> list[str]:
    return [str(m.get("content", "")) for m in transcript if m.get("role") == "tool"]


def _last_reply(transcript: list[Message]) -> tuple[dict[str, Any], str]:
    """The tool-role reply's JSON body and its ``[receipt: … status=…]`` line (#642/#944)."""
    replies = _tool_messages(transcript)
    assert replies, "the member's turn carries no tool-role reply to its call"
    body, _, receipt = replies[-1].partition("\n")
    return json.loads(body), receipt


async def test_an_injected_operation_never_reaches_the_registry_and_the_run_goes_on() -> None:
    registry = _Registry()
    executions = _Executions()
    llm = _AttackingLLM({"operation": "delete_repo", "repo": "a/b"})

    row = await _service(registry, executions, llm).execute(
        manifest_inline=_manifest(), manifest_ref=None, user_input="go", principal=_principal()
    )

    # 1. The registry saw ONE call, the bound operation, from the model's adapted second turn.
    assert registry.executed == [{"operation": "read_file", "repo": "a/b"}], (
        "the registry received the model's operation — the binding is advisory (#956)"
    )
    # 2. The run went on and the member answered — a refusal costs one turn, not the run.
    assert row.status == HarnessStatus.SUCCEEDED.value
    assert row.output == "The README says hello."


async def test_the_refusal_reaches_the_member_as_a_coded_tool_error() -> None:
    """The model must be TOLD, in the tool-role reply to ITS call id, in a form it can act on.
    A refusal that only reaches the operator trace leaves the member blind and the transcript
    invalid (a tool_call with no answering message)."""
    registry = _Registry()
    llm = _AttackingLLM({"operation": "delete_repo", "repo": "a/b"})

    await _service(registry, _Executions(), llm).execute(
        manifest_inline=_manifest(), manifest_ref=None, user_input="go", principal=_principal()
    )

    assert llm.turns == 3
    reply, receipt = _last_reply(llm.transcripts[1])
    assert _CODE in reply.get("detail", ""), reply
    assert "status=error" in receipt, receipt
    assert "delete_repo" not in json.dumps(reply), "the reply echoes the injected value"


async def test_the_step_trace_records_the_refusal_as_an_error_tool_step() -> None:
    """The persisted trace is what an operator and the run page read; the refusal must be
    visible there as an error on the tool step, with its code, not as a silent success."""
    registry = _Registry()
    executions = _Executions()
    llm = _AttackingLLM({"operation": "delete_repo", "repo": "a/b"})

    await _service(registry, executions, llm).execute(
        manifest_inline=_manifest(), manifest_ref=None, user_input="go", principal=_principal()
    )

    assert executions.created is not None
    steps = executions.created["steps"]
    tool_steps = [s for s in steps if s["kind"] == StepKind.TOOL.value]
    assert len(tool_steps) == 2, [s["status"] for s in tool_steps]
    refused, ran = tool_steps
    assert refused["status"] == "error"
    assert _CODE in (refused["detail"] or "")
    assert ran["status"] == "ok"


async def test_an_injected_operation_equal_to_the_bound_one_is_stripped_and_runs_once() -> None:
    """The positive half of the ruling: naming the operation the model was given changes
    nothing, so it is not refused — the registry sees the bound key exactly once."""
    registry = _Registry()
    llm = _AttackingLLM({"operation": "read_file", "repo": "a/b"})

    row = await _service(registry, _Executions(), llm).execute(
        manifest_inline=_manifest(), manifest_ref=None, user_input="go", principal=_principal()
    )

    assert registry.executed[0] == {"operation": "read_file", "repo": "a/b"}
    assert row.status == HarnessStatus.SUCCEEDED.value
    first_reply, receipt = _last_reply(llm.transcripts[1])
    assert "error" not in first_reply, first_reply
    assert "status=ok" in receipt, receipt
