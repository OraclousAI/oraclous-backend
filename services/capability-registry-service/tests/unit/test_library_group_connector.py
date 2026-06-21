"""Unit: the LibraryGroupExecutor — curated library functions as in-process operations (#488).

Decisive checks: each curated operation runs in-process and returns its dict output; an unknown or
missing operation is rejected; a wrong-typed (or bool) arg is INVALID_INPUT; the descriptor's
CAPABILITIES + operation enum are generated from the registry (never drift); the plugin is
registered and factory-resolvable.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_capability_registry_service.domain.connectors.library_group import (
    LibraryGroupExecutor,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import create_executor
from oraclous_capability_registry_service.domain.libraries.registry import operation_names
from oraclous_capability_registry_service.domain.plugins import plugin_registry
from oraclous_capability_registry_service.domain.plugins.builtin import LibraryGroupPlugin

pytestmark = pytest.mark.unit


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
    )


def _ex() -> LibraryGroupExecutor:
    return LibraryGroupExecutor({"id": "x"})


async def test_word_count() -> None:
    res = await _ex().execute({"operation": "word_count", "text": "the quick brown fox"}, _ctx())
    assert res.success and res.data == {"count": 4}
    assert res.metadata == {"operation": "word_count"}


async def test_to_upper() -> None:
    res = await _ex().execute({"operation": "to_upper", "text": "hello"}, _ctx())
    assert res.success and res.data == {"result": "HELLO"}


async def test_extract_emails_is_distinct_and_sorted() -> None:
    text = "ping a@x.test, then b@y.test and a@x.test again"
    res = await _ex().execute({"operation": "extract_emails", "text": text}, _ctx())
    assert res.success and res.data == {"emails": ["a@x.test", "b@y.test"]}


async def test_an_oversized_text_arg_is_rejected_before_dispatch() -> None:
    res = await _ex().execute({"operation": "to_upper", "text": "a" * 100_001}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_extract_emails_is_linear_on_adversarial_input() -> None:
    # The old quadratic regex took minutes on this kind of input; the bounded regex is instant, so
    # this test completing fast (no timeout) proves the catastrophic backtracking is gone. Kept just
    # under the 100k arg cap so the regex (not the cap) is what handles it.
    adversarial = "a@" + "." * 90_000
    res = await _ex().execute({"operation": "extract_emails", "text": adversarial}, _ctx())
    assert res.success and res.data == {"emails": []}


async def test_unknown_operation_is_rejected() -> None:
    res = await _ex().execute({"operation": "delete_everything", "text": "x"}, _ctx())
    assert not res.success and res.error_type == "INVALID_OPERATION"


async def test_missing_operation_is_rejected() -> None:
    res = await _ex().execute({"text": "x"}, _ctx())
    assert not res.success and res.error_type == "INVALID_OPERATION"


async def test_wrong_arg_type_is_invalid_input() -> None:
    res = await _ex().execute({"operation": "word_count", "text": 123}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_a_bool_arg_is_rejected_not_coerced() -> None:
    res = await _ex().execute({"operation": "to_upper", "text": True}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


def test_descriptor_capabilities_match_the_registry() -> None:
    desc = LibraryGroupPlugin.descriptor()
    cap_names = [c["name"] for c in desc["spec"]["capabilities"]]
    assert cap_names == operation_names()  # generated, never hand-maintained
    assert desc["spec"]["input_schema"]["properties"]["operation"]["enum"] == operation_names()


def test_plugin_is_registered_and_factory_resolves_it() -> None:
    assert LibraryGroupPlugin in set(plugin_registry.discover())
    assert isinstance(create_executor(LibraryGroupPlugin.descriptor()), LibraryGroupExecutor)
