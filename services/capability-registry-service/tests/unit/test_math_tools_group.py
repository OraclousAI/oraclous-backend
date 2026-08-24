"""Unit: ``math-tools`` reached as a tool group — separation, typing, and the generated descriptor.

``test_math_tools.py`` covers the arithmetic. This file covers everything between a member and that
arithmetic, and two of its checks pin decisions rather than mechanics. Both are called out in the
`[tests]` PR body for `be-test-reviewer`, because an implementer cannot infer either from the issue
text alone.

**A SEPARATE group, not more operations on the text one.** ``connectors/library_group.py`` says it
plainly: "a member binds the group token and gets every operation". So a single curated registry
means the member that was given a word counter is also handed a compounding function, and the
member doing unit economics is handed an e-mail extractor. The issue's own acceptance criterion 1
asks for "a ``math_tools`` library group", while its implementation sketch describes adding
operations to the existing registry — those are different things, and the tests here pin the first.
The existing plugin's name, description and tags are text-specific, which is the same conclusion
from the other direction.

**An integer is a number.** The dispatcher validates with ``isinstance(value, expected)`` and every
financial argument is naturally a ``float``, but ``isinstance(40000, float)`` is ``False``. So a
member sending a round figure — which is what a member sends — has its call rejected as
INVALID_INPUT. That is unreachable today because no curated operation takes a number; it becomes
reachable with the first one. Widening this is an implementation change to
``connectors/library_group.py``, not to the curated functions.

RED until the math library, its plugin and the numeric-argument handling land. Imports of the
not-yet-built seam are function-local (``.claude/rules/tests-seam-imports.md``) so a missing module
fails these tests at runtime instead of aborting collection for the whole suite.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_capability_registry_service.domain.connectors.library_group import (
    LibraryGroupExecutor,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import create_executor
from oraclous_capability_registry_service.domain.plugins import plugin_registry
from oraclous_capability_registry_service.domain.plugins.builtin import LibraryGroupPlugin

pytestmark = pytest.mark.unit

#: The operations this group must expose. The set is closed on purpose: an operation appearing here
#: without a ruled ``result_kind`` is caught by ``test_capability_result_kind.py``, and one
#: appearing there but not here would mean the group grew without a test.
MATH_OPERATIONS = {
    "percentage_change",
    "compound_growth",
    "break_even_units",
    "payback_period",
    "ratio",
}


def _plugin() -> Any:
    """The Math Tools plugin. Function-local: the seam lands with the `[impl]` PR."""
    from oraclous_capability_registry_service.domain.plugins.builtin import MathToolsPlugin

    return MathToolsPlugin


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
    )


def _math_executor() -> Any:
    """An executor resolved from the Math Tools descriptor, the way the platform resolves one."""
    return create_executor(_plugin().descriptor())


# ── the group exists and is reachable the same way every other tool group is ──────────────────────


def test_the_math_plugin_is_registered_and_factory_resolves_it() -> None:
    assert _plugin() in set(plugin_registry.discover())
    assert isinstance(_math_executor(), LibraryGroupExecutor)


def test_the_math_plugin_is_keyless() -> None:
    # Curated, in-process, no network: nothing to authenticate, so a member can use it with no
    # credential set up at all. This is most of why the capability is cheap to adopt.
    assert _plugin().CREDENTIAL_REQUIREMENTS == []


def test_the_descriptor_is_generated_from_the_registry_not_hand_written() -> None:
    # The same no-drift property the text group has: the advertised capabilities and the operation
    # enum both come from the registry, so a function and its descriptor cannot disagree.
    desc = _plugin().descriptor()
    cap_names = [c["name"] for c in desc["spec"]["capabilities"]]
    assert set(cap_names) == MATH_OPERATIONS
    assert set(desc["spec"]["input_schema"]["properties"]["operation"]["enum"]) == MATH_OPERATIONS


def test_every_math_argument_is_declared_on_the_input_schema() -> None:
    # Acceptance criterion: the schema must name the new arguments, or a member's call is rejected
    # by validation before it ever reaches the function.
    props = _plugin().descriptor()["spec"]["input_schema"]["properties"]
    for arg in (
        "start",
        "end",
        "rate",
        "periods",
        "fixed_costs",
        "price_per_unit",
        "variable_cost_per_unit",
        "initial_investment",
        "cash_flow_per_period",
        "numerator",
        "denominator",
        "numerator_unit",
        "denominator_unit",
    ):
        assert arg in props, f"{arg} is not declared on the math input schema"


# ── the separation itself ────────────────────────────────────────────────────────────────────────


def test_the_text_group_does_not_gain_the_math_operations() -> None:
    # The decision this file pins. Binding a word counter must not also hand a member a compounding
    # function: a member gets every operation in the group it binds.
    text_ops = {c["name"] for c in LibraryGroupPlugin.descriptor()["spec"]["capabilities"]}
    assert not (text_ops & MATH_OPERATIONS), (
        f"math operations leaked into the text group: {sorted(text_ops & MATH_OPERATIONS)}"
    )


def test_the_math_group_does_not_gain_the_text_operations() -> None:
    math_ops = {c["name"] for c in _plugin().descriptor()["spec"]["capabilities"]}
    assert not (math_ops & {"word_count", "to_upper", "extract_emails"})


def test_the_two_groups_have_distinct_names() -> None:
    # The slug is derived from the name and is what a binding resolves against, so two groups
    # sharing a name is not a cosmetic problem.
    assert _plugin().NAME != LibraryGroupPlugin.NAME


async def test_a_text_operation_is_rejected_on_the_math_group() -> None:
    # Separation has to hold at dispatch, not only in the advertised list: an operation the group
    # does not own is unknown to it, however it is reached.
    res = await _math_executor().execute({"operation": "word_count", "text": "a b"}, _ctx())
    assert not res.success and res.error_type == "INVALID_OPERATION"


# ── dispatch: numbers ────────────────────────────────────────────────────────────────────────────


async def test_a_math_operation_runs_and_returns_its_dict() -> None:
    res = await _math_executor().execute(
        {"operation": "compound_growth", "start": 100.0, "rate": 0.5, "periods": 2}, _ctx()
    )
    assert res.success
    assert res.data["value"] == 225.0
    assert res.metadata == {"operation": "compound_growth"}


async def test_a_whole_number_is_accepted_where_a_number_is_expected() -> None:
    # The second pinned decision. A member sends 40000, not 40000.0 — round figures are what money
    # arguments look like. `isinstance(40000, float)` is False, so today's validator rejects this
    # as INVALID_INPUT, and every realistic call fails. Widening it lives in the dispatcher.
    res = await _math_executor().execute(
        {"operation": "compound_growth", "start": 40000, "rate": 0.08, "periods": 18}, _ctx()
    )
    assert res.success, f"a whole number was rejected: {res.error_message}"
    assert res.data["value"] == pytest.approx(159840.7819666870, abs=1e-6)


async def test_a_bool_is_still_rejected_where_a_number_is_expected() -> None:
    # Widening int-for-float must not widen bool-for-number: `bool` is an `int` subclass, so the
    # existing explicit bool rejection is exactly what stops True arriving as 1.
    res = await _math_executor().execute(
        {"operation": "compound_growth", "start": True, "rate": 0.5, "periods": 2}, _ctx()
    )
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_a_string_is_rejected_where_a_number_is_expected() -> None:
    res = await _math_executor().execute(
        {"operation": "compound_growth", "start": "100", "rate": 0.5, "periods": 2}, _ctx()
    )
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_a_missing_numeric_argument_is_rejected() -> None:
    res = await _math_executor().execute({"operation": "compound_growth", "start": 100}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_a_typed_arithmetic_error_reaches_the_caller_as_data() -> None:
    # An undefined calculation is not a tool failure: the call itself worked. The member needs to
    # read "this product never breaks even" and say so, rather than seeing an opaque error.
    res = await _math_executor().execute(
        {
            "operation": "break_even_units",
            "fixed_costs": 50000,
            "price_per_unit": 40,
            "variable_cost_per_unit": 45,
        },
        _ctx(),
    )
    assert res.success
    assert res.data["error"] == "no_contribution"


async def test_an_unbounded_period_count_is_refused_rather_than_absorbed() -> None:
    # The dispatcher runs curated functions in a thread it cannot kill, and its only existing guard
    # caps STRING length. This is the numeric equivalent, and it must hold through dispatch rather
    # than only in the function.
    res = await _math_executor().execute(
        {"operation": "compound_growth", "start": 2, "rate": 1.0, "periods": 10**9}, _ctx()
    )
    assert res.success
    assert res.data["error"] == "periods_out_of_range"
