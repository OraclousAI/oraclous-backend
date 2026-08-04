"""Unit: a builtin connector rejects a malformed call with a repairable message (#693).

`GithubSinkConnector` declares an `INPUT_SCHEMA` whose `files[]` items require `path` and `content`,
and nothing enforces it: `github_sink.py` subscripts `f["path"]` over whatever the caller sent. On
team run `62201877-729c-4a4a-a2ee-3d4fe8c3fcdb` the `Commenter` member's `files` entries had no
`path`, so the raw `KeyError` message became the whole error detail the model saw:

    {"error": "RegistryError", "detail": "tool execution failed: 'path'"}

`'path'` names no argument, no item index and no expectation, so the model could not repair the call
and reissued the identical one ten times before giving up.

The declared schema is the fix that is already half-present. These tests pin it at the **shared**
seam rather than in one connector: every builtin executor is an `InternalTool`, so validating inside
`InternalTool.execute` — against the descriptor the executor was built from — closes the class for
every builtin at once (#693 AC5, the audit bullet), not just for the sink.

Two not-yet-built seams, imported FUNCTION-LOCALLY (`.claude/rules/tests-seam-imports.md`):
* `domain.executors.input_validation.validate_input` — the schema check itself.
* `InternalTool.execute` calling it, plus its no-leak mapping of a raw builtin exception.

RED until the #693 [impl] lands.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)
from oraclous_capability_registry_service.domain.executors.factory import (
    create_executor,
    has_executor,
)
from oraclous_capability_registry_service.domain.plugins.base import plugin_registry
from oraclous_capability_registry_service.domain.plugins.builtin import GitHubSinkPlugin

pytestmark = pytest.mark.unit

_REPO = "octo/book"


def _ctx(**configuration: Any) -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        configuration=dict(configuration),
        credentials={"api_key": {"api_key": "ghp_dummy"}},
    )


def _sink():  # noqa: ANN202 — the executor type is resolved by the factory
    """The real sink executor built from its real descriptor (the schema must come from there)."""
    return create_executor(GitHubSinkPlugin.descriptor())


# ── the seam itself ───────────────────────────────────────────────────────────────────────────────


def test_validate_input_names_the_nested_argument_path() -> None:
    """The message must name `files[0].path` — the argument path a model can act on."""
    from oraclous_capability_registry_service.domain.executors.input_validation import (
        validate_input,
    )

    problem = validate_input(
        GitHubSinkPlugin.INPUT_SCHEMA,
        {"operation": "deliver", "repo": _REPO, "files": [{"content": "hello"}]},
    )
    assert problem is not None
    assert "files[0].path" in problem
    assert "required" in problem.lower()


def test_validate_input_names_a_wrong_type_and_what_was_expected() -> None:
    from oraclous_capability_registry_service.domain.executors.input_validation import (
        validate_input,
    )

    problem = validate_input(
        GitHubSinkPlugin.INPUT_SCHEMA,
        {"operation": "deliver", "repo": _REPO, "files": [{"path": "a.md", "content": 7}]},
    )
    assert problem is not None
    assert "files[0].content" in problem
    assert "string" in problem


def test_validate_input_reports_the_second_item_by_its_own_index() -> None:
    """A model repairing the call needs the failing item's index, not just the field name."""
    from oraclous_capability_registry_service.domain.executors.input_validation import (
        validate_input,
    )

    problem = validate_input(
        GitHubSinkPlugin.INPUT_SCHEMA,
        {
            "operation": "deliver",
            "repo": _REPO,
            "files": [{"path": "a.md", "content": "ok"}, {"content": "no path"}],
        },
    )
    assert problem is not None
    assert "files[1].path" in problem


def test_validate_input_accepts_a_well_formed_call() -> None:
    from oraclous_capability_registry_service.domain.executors.input_validation import (
        validate_input,
    )

    assert (
        validate_input(
            GitHubSinkPlugin.INPUT_SCHEMA,
            {
                "operation": "deliver",
                "repo": _REPO,
                "files": [{"path": "a.md", "content": "hello"}],
            },
        )
        is None
    )


def test_a_top_level_required_field_may_be_bound_on_the_instance_configuration() -> None:
    """`repo` is `required` in the sink's INPUT_SCHEMA but the connector also accepts it bound on
    the instance configuration (the "configured, not passed" shape, #542). Validation must honour
    that, or every configured sink instance starts failing a call that works today."""
    from oraclous_capability_registry_service.domain.executors.input_validation import (
        validate_input,
    )

    call = {"operation": "deliver", "files": [{"path": "a.md", "content": "hello"}]}
    assert validate_input(GitHubSinkPlugin.INPUT_SCHEMA, call) is not None  # nothing supplies repo
    assert (
        validate_input(GitHubSinkPlugin.INPUT_SCHEMA, call, configuration={"repo": _REPO}) is None
    )


# ── the regression the issue was filed on ─────────────────────────────────────────────────────────


async def test_deliver_without_path_returns_a_typed_validation_error_not_a_keyerror() -> None:
    """#693 AC4 — the exact failing call from run 62201877-729c-4a4a-a2ee-3d4fe8c3fcdb."""
    result = await _sink().execute(
        {"operation": "deliver", "repo": _REPO, "files": [{"content": "the review"}]},
        _ctx(),
    )
    assert not result.success
    assert result.error_type == "INVALID_INPUT"
    detail = result.error_message or ""
    assert "files[0].path" in detail
    assert detail != "'path'"  # the bare KeyError message the member actually received
    assert "KeyError" not in detail


async def test_deliver_validation_runs_before_any_forge_call() -> None:
    """A malformed call must be rejected with no network at all — the sink has no injected
    transport here, so reaching the forge would raise instead of returning a typed result."""
    result = await _sink().execute(
        {"operation": "deliver", "repo": _REPO, "files": [{"content": "x"}]}, _ctx()
    )
    assert result.error_type == "INVALID_INPUT"


# ── the class, not the instance: no raw builtin exception message reaches a caller ────────────────


class _RawSubscript(InternalTool):
    """Stands in for any connector that subscripts an unvalidated key (the #693 shape)."""

    async def _execute_internal(self, input_data, context) -> ExecutionResult:  # noqa: ANN001, ARG002
        return ExecutionResult(success=True, data={"path": input_data["path"]})


class _RawIndex(InternalTool):
    async def _execute_internal(self, input_data, context) -> ExecutionResult:  # noqa: ANN001, ARG002
        return ExecutionResult(success=True, data={"first": input_data["items"][0]})


@pytest.mark.parametrize(
    ("executor_cls", "call"),
    [(_RawSubscript, {}), (_RawIndex, {"items": []})],
)
async def test_a_raw_builtin_exception_message_never_becomes_the_error_detail(
    executor_cls: type[InternalTool], call: dict[str, Any]
) -> None:
    """#693 AC2. `str(KeyError('path'))` is `"'path'"` and `str(IndexError(...))` is
    `'list index out of range'` — neither tells a caller which argument to fix, and a KeyError's
    message is caller-supplied data. Neither may be surfaced verbatim as the detail."""
    result = await executor_cls({}).execute(call, _ctx())
    assert not result.success
    detail = result.error_message or ""
    assert detail not in ("'path'", "list index out of range")
    assert len(detail) > 20  # a curated sentence, not a quoted key name


async def test_every_builtin_executor_rejects_an_empty_call_against_its_own_schema() -> None:
    """#693 AC5 — the audit bullet, made structural. Every builtin plugin that declares required
    input and has a registered executor must reject an empty call with a typed INVALID_INPUT naming
    a missing field. Validation runs before `_execute_internal`, so no connector does any I/O here.
    """
    checked = 0
    for plugin in plugin_registry.discover():
        schema = getattr(plugin, "INPUT_SCHEMA", None)
        if not isinstance(schema, dict) or not schema.get("required"):
            continue
        descriptor = plugin.descriptor()
        if not has_executor(descriptor):
            continue
        result = await create_executor(descriptor).execute({}, _ctx())
        assert not result.success, f"{plugin.__name__} accepted an empty call"
        assert result.error_type == "INVALID_INPUT", f"{plugin.__name__} → {result.error_type}"
        first_required = schema["required"][0]
        assert first_required in (result.error_message or ""), (
            f"{plugin.__name__} did not name the missing '{first_required}'"
        )
        checked += 1
    assert checked >= 10, f"only {checked} builtin executors were covered by the audit"
