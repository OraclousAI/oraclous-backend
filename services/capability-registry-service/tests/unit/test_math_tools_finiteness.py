"""Unit: a finite input pair whose RESULT is not finite (#822, found at code review).

``test_math_tools.py`` pins totality against bad ARGUMENTS. This file pins the other half, which a
live review found reaching the gateway as a 500: every argument can be a finite, JSON-legal number
and the result still overflow to infinity. ``1e308`` doubled is one route; a division by a denormal
denominator is the other, and it needs no large numerator at all.

Infinity is not a JSON number, so an unguarded result leaves the service as an INTERNAL_ERROR the
member can neither read nor act on — the exact opposite of the typed-error contract the module
docstring states. ``break_even_units`` is worse still: ``math.ceil(inf)`` raises ``OverflowError``,
so the execution comes back FAILED with an opaque exception name.

Every case here uses ordinary finite arguments. The period bound does not help with any of them —
in the compounding case ``periods`` is 1.
"""

from __future__ import annotations

import math

import pytest

pytestmark = pytest.mark.unit


def _math() -> object:
    from oraclous_capability_registry_service.domain.libraries import math_tools

    return math_tools


def test_compounding_past_the_representable_range_is_a_typed_error() -> None:
    # The reproduction from the review, verbatim: one period, a rate of 1, a start near the top of
    # the double range. Nothing about the inputs is hostile or unusual in shape.
    out = _math().compound_growth(start=1e308, rate=1, periods=1)
    assert out["error"] == "value_out_of_range"
    assert "detail" in out


def test_a_break_even_that_overflows_is_a_typed_error_not_a_raise() -> None:
    # `math.ceil(inf)` raises OverflowError, so the guard has to run BEFORE the rounding. A test
    # that only checked the returned dict would pass against an implementation that raises here.
    out = _math().break_even_units(
        fixed_costs=1e308, price_per_unit=1e-320, variable_cost_per_unit=0
    )
    assert out["error"] == "value_out_of_range"


def test_a_ratio_over_a_denormal_denominator_is_a_typed_error() -> None:
    # No large numerator needed: dividing by a denormal is enough to leave the range.
    assert (
        _math().ratio(
            numerator=1e300,
            denominator=5e-324,
            numerator_unit="USD",
            denominator_unit="customer",
        )["error"]
        == "value_out_of_range"
    )


def test_a_percentage_change_from_a_denormal_base_is_a_typed_error() -> None:
    # A base that is not zero — so `undefined_base` does not catch it — but small enough that the
    # division leaves the range.
    assert _math().percentage_change(start=5e-324, end=1e300)["error"] == "value_out_of_range"


def test_a_payback_over_a_denormal_cash_flow_is_a_typed_error() -> None:
    # The cash flow is positive, so `no_payback` does not catch it.
    assert (
        _math().payback_period(initial_investment=1e300, cash_flow_per_period=5e-324)["error"]
        == "value_out_of_range"
    )


def test_no_operation_ever_returns_a_value_that_is_not_finite() -> None:
    """The property behind all five cases above, stated once.

    A number a JSON encoder cannot represent must never reach a result dict, whatever the route
    that produced it. This is the check a sixth operation added later inherits for free.
    """
    math_tools = _math()
    overflowing: list[tuple[str, dict]] = [
        ("percentage_change", {"start": 5e-324, "end": 1e300}),
        ("compound_growth", {"start": 1e308, "rate": 1, "periods": 1}),
        (
            "break_even_units",
            {"fixed_costs": 1e308, "price_per_unit": 1e-320, "variable_cost_per_unit": 0},
        ),
        ("payback_period", {"initial_investment": 1e300, "cash_flow_per_period": 5e-324}),
        (
            "ratio",
            {
                "numerator": 1e300,
                "denominator": 5e-324,
                "numerator_unit": "USD",
                "denominator_unit": "customer",
            },
        ),
    ]
    for name, kwargs in overflowing:
        result = getattr(math_tools, name)(**kwargs)
        for key, value in result.items():
            if isinstance(value, float):
                assert math.isfinite(value), f"{name}.{key} is not a representable number"
