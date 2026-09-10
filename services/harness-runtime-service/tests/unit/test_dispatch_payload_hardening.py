"""Unit (#1004, items 2-4): the three residual holes in ``dispatch_payload``.

#956 bound the operation at dispatch. An independent verification on ``main`` found three things
the binding does not cover, none exploitable for the #956 threat today, all defence in depth:

2. ``additionalProperties: false`` on a first-party operation's schema is a hint to the PROVIDER —
   nothing on our side re-validates the model's arguments, so an undeclared key alongside a correct
   ``operation`` reaches the registry payload untouched. Ruled: it STAYS advisory (real enforcement
   is #898 / #911), but the runtime must say when it happens — at WARNING, naming the KEYS and
   never the values, capped at 5 names / 64 characters, and only for a schema that actually closed
   itself (an open schema declares extra keys legal, so there is nothing to report).
3. The operation key was matched exactly, so ``Operation`` / ``OPERATION`` rode through as ordinary
   extra keys — harmless only because the sampled connectors read the lowercase spelling. Ruled:
   any key that lowercases to ``operation`` is THE operation key for the strip/refuse decision.
4. A non-dict ``args`` raised an incidental ``TypeError`` from the dict comprehension, caught by
   the loop's broad handler. Fail-closed by accident is not fail-closed by contract. Ruled: an
   explicit coded refusal, a sibling of ``OperationOverrideRefused``.

RED until the #1004 ``[impl]`` lands.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from oraclous_harness_runtime_service.domain.llm.base import ToolSpec
from oraclous_harness_runtime_service.domain.tool_schemas import (
    OperationOverrideRefused,
    dispatch_payload,
)

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.tool_dispatch]

#: How many unknown key NAMES a single warning may carry, and how long the rendered list may be.
_MAX_NAMES = 5
_MAX_NAME_CHARS = 64


def _closed_spec(properties: dict[str, Any] | None = None) -> ToolSpec:
    """A first-party operation's spec: the schema closed itself (#956 ruling 2)."""
    return ToolSpec(
        name="gh__read_file",
        description="Read a file",
        parameters={
            "type": "object",
            "properties": properties if properties is not None else {"repo": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        },
        binding="gh",
        operation="read_file",
    )


def _open_spec() -> ToolSpec:
    """An imported MCP operation's schema is the SERVER's contract, passed through as it came
    (#698 D1) — open, or with no ``additionalProperties`` at all."""
    return ToolSpec(
        name="acme__create_issue",
        description="Open an issue",
        parameters={"type": "object", "properties": {"title": {"type": "string"}}},
        binding="acme",
        operation="create_issue",
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# --- item 2: the closed schema is advisory, and an undeclared key is REPORTED --------------------


def test_an_undeclared_key_still_reaches_the_registry_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ruling: enforcement stays out (it belongs with #898 / #911). The key travels; only the
    log changes. Silently dropping it would break any tool whose real contract is wider than the
    hint map the schema was generated from."""
    with caplog.at_level(logging.WARNING):
        payload = dispatch_payload(_closed_spec(), {"repo": "a/b", "sneaky": "x"})

    assert payload == {"operation": "read_file", "repo": "a/b", "sneaky": "x"}


def test_an_undeclared_key_is_logged_at_warning_by_name(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        dispatch_payload(_closed_spec(), {"repo": "a/b", "sneaky": "x"})

    texts = _warnings(caplog)
    assert texts, "no WARNING was logged for a key the closed schema did not declare"
    assert any("sneaky" in t for t in texts), "the WARNING does not name the undeclared key"


def test_the_warning_never_carries_the_undeclared_keys_VALUE(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A value is model-authored content; a key NAME is what an operator needs to diagnose. The
    same discipline the #956 mismatch log already follows."""
    written_by_the_model = "the-model-wrote-this-value"
    with caplog.at_level(logging.WARNING):
        dispatch_payload(_closed_spec(), {"repo": "a/b", "sneaky": written_by_the_model})

    assert all(written_by_the_model not in t for t in _warnings(caplog))


def test_the_warning_caps_the_number_of_names(caplog: pytest.LogCaptureFixture) -> None:
    """A model that returns forty undeclared keys must not get forty of them into a log line."""
    args = {"repo": "a/b", **{f"k{i:02d}": i for i in range(40)}}
    with caplog.at_level(logging.WARNING):
        dispatch_payload(_closed_spec(), args)

    texts = _warnings(caplog)
    assert texts
    named = [k for k in args if k != "repo" and k in " ".join(texts)]
    assert len(named) <= _MAX_NAMES, f"more than {_MAX_NAMES} undeclared key names were logged"


def test_the_warning_caps_the_rendered_name_list(caplog: pytest.LogCaptureFixture) -> None:
    """A key name is itself model-supplied text, so the rendered list is bounded as well as
    counted — otherwise five 10,000-character names are the channel the count was meant to close."""
    long_names = {("n" * 400) + str(i): 1 for i in range(3)}
    with caplog.at_level(logging.WARNING):
        dispatch_payload(_closed_spec(), {"repo": "a/b", **long_names})

    texts = _warnings(caplog)
    assert texts
    assert all("n" * (_MAX_NAME_CHARS + 1) not in t for t in texts), (
        f"the WARNING renders more than {_MAX_NAME_CHARS} characters of undeclared key names"
    )


def test_an_open_schema_reports_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """An imported server's open schema DECLARES that extra keys are legal (#698 D1). Warning on
    every argument of every MCP tool would be noise, not a signal."""
    with caplog.at_level(logging.WARNING):
        payload = dispatch_payload(_open_spec(), {"title": "x", "labels": ["bug"]})

    assert payload == {"operation": "create_issue", "title": "x", "labels": ["bug"]}
    assert _warnings(caplog) == []


def test_a_fully_declared_call_reports_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        dispatch_payload(_closed_spec(), {"repo": "a/b"})

    assert _warnings(caplog) == []


def test_the_bound_operation_key_is_not_an_undeclared_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A model that echoes the operation it was given is stripped, not reported: the key is the
    runtime's own routing field, never one the tool's schema was supposed to declare."""
    with caplog.at_level(logging.WARNING):
        payload = dispatch_payload(_closed_spec(), {"operation": "read_file", "repo": "a/b"})

    assert payload == {"operation": "read_file", "repo": "a/b"}
    assert _warnings(caplog) == [], "the routing key was reported as an undeclared argument"


# --- item 3: any case variant of the key IS the operation key ------------------------------------


@pytest.mark.parametrize("key", ["Operation", "OPERATION", "oPeRaTiOn"])
def test_a_case_variant_holding_a_different_operation_is_refused(key: str) -> None:
    """``{"Operation": "delete_repo"}`` used to ride through as an ordinary extra key. Whether a
    given connector happens to read it is not the runtime's business — the binding decides, and a
    key that means "pick the operation" is refused however it is spelled."""
    with pytest.raises(OperationOverrideRefused):
        dispatch_payload(_closed_spec(), {key: "delete_repo", "repo": "a/b"})


@pytest.mark.parametrize("key", ["Operation", "OPERATION"])
def test_a_case_variant_holding_the_bound_operation_is_stripped(key: str) -> None:
    """Same rule as the exact-case key: naming the operation you were in fact given changes
    nothing, so it is stripped and the call proceeds — and the payload carries the key ONCE, in
    the runtime's own lowercase spelling."""
    payload = dispatch_payload(_closed_spec(), {key: "read_file", "repo": "a/b"})

    assert payload == {"operation": "read_file", "repo": "a/b"}


def test_a_case_variant_is_still_matched_exactly_on_its_VALUE() -> None:
    """Folding the KEY is not folding the value: ``READ_FILE`` is not ``read_file`` (#956)."""
    with pytest.raises(OperationOverrideRefused):
        dispatch_payload(_closed_spec(), {"Operation": "READ_FILE", "repo": "a/b"})


def test_two_spellings_that_disagree_are_refused() -> None:
    """One says the bound operation, the other says something else. Fail-closed: a payload that
    contains two different answers to "which operation" is refused, never resolved by ordering."""
    with pytest.raises(OperationOverrideRefused):
        dispatch_payload(
            _closed_spec(), {"operation": "read_file", "Operation": "delete_repo", "repo": "a/b"}
        )


def test_a_nested_case_variant_is_the_tools_own_argument() -> None:
    """The strip stays SHALLOW (#698 D3): a nested key belongs to the tool's input, whatever its
    case."""
    nested = {"repo": "a/b", "filters": {"Operation": "delete_repo"}}

    assert dispatch_payload(_closed_spec(), nested) == {"operation": "read_file", **nested}


# --- item 4: non-dict args refuse by contract, not by TypeError ----------------------------------


@pytest.mark.parametrize("args", [None, [], ["read_file"], "read_file", 1, True, 0.5])
def test_non_object_arguments_raise_the_coded_refusal(args: object) -> None:
    """A model can return a JSON array or a bare string where an object was asked for. Today that
    is a ``TypeError`` from a dict comprehension — the loop's broad handler turns it into a generic
    tool error that names nothing the model can fix (#693's lesson)."""
    from oraclous_harness_runtime_service.domain.tool_schemas import ToolDispatchRefused

    with pytest.raises(ToolDispatchRefused) as ei:
        dispatch_payload(_closed_spec(), args)  # type: ignore[arg-type]

    assert not isinstance(ei.value, TypeError)


def test_the_non_object_refusal_is_coded_and_bounded() -> None:
    """``str(exc)`` is the ``detail`` the loop feeds back to the model, so it carries a token the
    model can act on and never the arguments themselves."""
    from oraclous_harness_runtime_service.domain.tool_schemas import (
        NON_OBJECT_ARGUMENTS_REFUSED,
        ToolDispatchRefused,
    )

    injected = "w" * 3000
    with pytest.raises(ToolDispatchRefused) as ei:
        dispatch_payload(_closed_spec(), injected)  # type: ignore[arg-type]

    detail = str(ei.value)
    assert NON_OBJECT_ARGUMENTS_REFUSED in detail
    assert injected not in detail
    assert len(detail) <= 300, "the refusal is not bounded by the run page's per-step budget"


def test_the_operation_override_refusal_shares_the_same_base() -> None:
    """Both refusals are the same kind of event — the call was rejected before the registry — so
    the dispatch closure can catch and log them under one name."""
    from oraclous_harness_runtime_service.domain.tool_schemas import ToolDispatchRefused

    assert issubclass(OperationOverrideRefused, ToolDispatchRefused)
