"""Unit: the curated ``math-tools`` library — deterministic arithmetic for a deliverable (#822).

Nothing in the catalogue computes a number today. The whole curated library is ``text_tools`` —
``word_count``, ``to_upper``, ``extract_emails`` — so every figure in every deliverable is written
by a language model in prose, and nothing in the run ever recomputes it. A unit-economics model, a
break-even, a payback period: all of them currently rest on a model's arithmetic, which is the
defect this issue exists to remove.

These functions are the arithmetic itself, tested as pure functions. ``test_math_tools_group.py``
covers how they are reached as a tool.

Four properties, and each is a rule for the next function added here:

**Exact, not approximate.** The point of the capability is that the same inputs give the same
number every time. Where a value is exactly representable the assertion is exact equality; where
it is not, the tolerance is tight enough that a wrong formula fails.

**Total.** Acceptance criterion 3: no exceptions on bad input, a typed error result instead. Every
undefined case — a zero base, a product that never breaks even, an investment that never pays back
— returns ``{"error": <code>, "detail": ...}``. A function here that raises is a bug, because the
dispatcher would turn it into an opaque tool failure rather than something a member can read and
act on.

**Bounded.** Dispatch is ``asyncio.to_thread`` and **a runaway thread cannot be killed**
(``connectors/library_group.py``). The existing 100k cap covers string arguments only, so a
compounding call with a huge period count is an unbounded CPU and memory cost reachable from a
curated, keyless, in-process operation. ``periods`` is bounded here, in the function, and the bound
is a typed error like any other bad input.

**Pure.** No I/O, no clock, no randomness — same arguments, same dict, forever.

RED until ``domain/libraries/math_tools.py`` exists. The import is function-local on purpose
(``.claude/rules/tests-seam-imports.md``): a module-level import of a seam that does not exist yet
aborts collection for the whole suite and reddens every other open PR.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _math() -> object:
    """The curated math library. Function-local: the seam lands with the `[impl]` PR."""
    from oraclous_capability_registry_service.domain.libraries import math_tools

    return math_tools


# ── percentage_change ────────────────────────────────────────────────────────────────────────────


def test_percentage_change_is_exact() -> None:
    assert _math().percentage_change(start=40000, end=52000) == {
        "percent": 30.0,
        "start": 40000,
        "end": 52000,
    }


def test_percentage_change_is_signed() -> None:
    # A fall is negative, not an absolute magnitude. Prose routinely loses this sign.
    assert _math().percentage_change(start=200, end=150)["percent"] == -25.0


def test_percentage_change_from_a_zero_base_is_a_typed_error() -> None:
    # Growth from nothing has no percentage. The undefined case must be nameable by the member that
    # called it, so it is a value, not an exception.
    out = _math().percentage_change(start=0, end=5)
    assert out["error"] == "undefined_base"
    assert "detail" in out


# ── compound_growth ──────────────────────────────────────────────────────────────────────────────


def test_compound_growth_compounds_rather_than_multiplying() -> None:
    # The failure this pins: 100 grown 50% twice is 225, not 200. A model doing this in prose
    # applies the rate once, or applies it linearly, and the wrong figure ships.
    assert _math().compound_growth(start=100, rate=0.5, periods=2)["value"] == 225.0


def test_compound_growth_matches_the_worked_example_on_the_issue() -> None:
    # 40,000/month at 8% a month for 18 months. The issue's illustration of a model answering
    # "roughly 151,000" in prose when the real figure is nearly 9,000 higher.
    out = _math().compound_growth(start=40000, rate=0.08, periods=18)
    assert out["value"] == pytest.approx(159840.7819666870, abs=1e-6)


def test_compound_growth_accepts_a_negative_rate() -> None:
    # Decay is the same arithmetic; churn and price erosion are the common cases.
    assert _math().compound_growth(start=1000, rate=-0.1, periods=2)["value"] == pytest.approx(
        810.0
    )


def test_compound_growth_over_zero_periods_returns_the_start() -> None:
    assert _math().compound_growth(start=1234.5, rate=0.2, periods=0)["value"] == 1234.5


def test_compound_growth_refuses_an_unbounded_period_count() -> None:
    # THE reason this bound exists: dispatch runs the function in a thread that cannot be killed,
    # and the existing argument cap covers string length only. `start ** periods` with a huge
    # exponent is an unbounded CPU and memory cost reachable through a keyless, in-process,
    # curated operation — so the function refuses it rather than the platform absorbing it.
    out = _math().compound_growth(start=2, rate=1.0, periods=10**9)
    assert out["error"] == "periods_out_of_range"
    assert "detail" in out


def test_compound_growth_refuses_a_negative_period_count() -> None:
    assert _math().compound_growth(start=100, rate=0.1, periods=-1)["error"] == (
        "periods_out_of_range"
    )


# ── break_even_units ─────────────────────────────────────────────────────────────────────────────


def test_break_even_units_returns_both_the_exact_and_the_whole_figure() -> None:
    # 50,000 of fixed cost, 75 of contribution per unit -> 666.67 exactly, 667 units in practice.
    # Both are returned deliberately: the exact value is the arithmetic, and the whole number is
    # what a decision actually needs. Leaving the rounding to the caller is how a model rounds it
    # DOWN to 666 and reports a break-even that does not break even.
    out = _math().break_even_units(fixed_costs=50000, price_per_unit=120, variable_cost_per_unit=45)
    assert out["units"] == pytest.approx(666.6666666666666)
    assert out["units_whole"] == 667


def test_break_even_units_rounds_up_only_when_there_is_a_remainder() -> None:
    # An exact division must not be pushed to the next unit.
    out = _math().break_even_units(fixed_costs=1000, price_per_unit=30, variable_cost_per_unit=20)
    assert out["units"] == 100.0
    assert out["units_whole"] == 100


def test_break_even_units_reports_a_product_that_never_breaks_even() -> None:
    # Price at or below variable cost: every additional unit loses money, so there is no break-even
    # point at all. Silently returning a negative or infinite unit count is how that reaches a
    # deliverable as a plausible number.
    out = _math().break_even_units(fixed_costs=50000, price_per_unit=40, variable_cost_per_unit=45)
    assert out["error"] == "no_contribution"
    assert "detail" in out


def test_break_even_units_treats_zero_contribution_as_no_break_even() -> None:
    assert (
        _math().break_even_units(fixed_costs=1, price_per_unit=10, variable_cost_per_unit=10)[
            "error"
        ]
        == "no_contribution"
    )


# ── payback_period ───────────────────────────────────────────────────────────────────────────────


def test_payback_period_is_exact() -> None:
    out = _math().payback_period(initial_investment=250000, cash_flow_per_period=40000)
    assert out["periods"] == 6.25


def test_payback_period_reports_an_investment_that_never_pays_back() -> None:
    # A zero or negative periodic cash flow never recovers the investment. This is the case a model
    # is most likely to paper over, because "never" is not a number.
    out = _math().payback_period(initial_investment=250000, cash_flow_per_period=0)
    assert out["error"] == "no_payback"
    assert "detail" in out


def test_payback_period_rejects_a_negative_investment() -> None:
    assert (
        _math().payback_period(initial_investment=-1, cash_flow_per_period=10)["error"]
        == "invalid_investment"
    )


# ── ratio ────────────────────────────────────────────────────────────────────────────────────────


def test_ratio_carries_its_units_into_the_result() -> None:
    # The units are the point. A bare 150.0 is exactly the kind of figure that gets relabelled on
    # the way into a document; "USD per customer" travels with the number that earned it.
    out = _math().ratio(
        numerator=180000, denominator=1200, numerator_unit="USD", denominator_unit="customer"
    )
    assert out["value"] == 150.0
    assert out["unit"] == "USD per customer"


def test_ratio_by_zero_is_a_typed_error() -> None:
    out = _math().ratio(
        numerator=1, denominator=0, numerator_unit="USD", denominator_unit="customer"
    )
    assert out["error"] == "division_by_zero"
    assert "detail" in out


# ── properties every operation here must hold ────────────────────────────────────────────────────


def _calls() -> list[tuple[str, dict]]:
    """One representative call per operation — the table the property tests below iterate."""
    return [
        ("percentage_change", {"start": 40000, "end": 52000}),
        ("compound_growth", {"start": 100, "rate": 0.5, "periods": 2}),
        (
            "break_even_units",
            {"fixed_costs": 50000, "price_per_unit": 120, "variable_cost_per_unit": 45},
        ),
        ("payback_period", {"initial_investment": 250000, "cash_flow_per_period": 40000}),
        (
            "ratio",
            {
                "numerator": 180000,
                "denominator": 1200,
                "numerator_unit": "USD",
                "denominator_unit": "customer",
            },
        ),
    ]


def test_every_operation_returns_a_dict() -> None:
    # The dispatcher hands the return value straight out as the execution's output_data, so a
    # non-dict return is a broken execution rather than a broken function.
    math_tools = _math()
    for name, kwargs in _calls():
        assert isinstance(getattr(math_tools, name)(**kwargs), dict), name


def test_every_operation_is_deterministic() -> None:
    # The whole justification for the capability: same inputs, same number, every time. A function
    # that reached for a clock, a random source or any I/O would fail here.
    math_tools = _math()
    for name, kwargs in _calls():
        func = getattr(math_tools, name)
        assert func(**kwargs) == func(**kwargs), name


def test_no_operation_raises_on_a_bad_argument() -> None:
    # Acceptance criterion 3, as a property rather than a per-function assertion: every operation
    # is TOTAL. A raise here becomes an opaque tool failure the calling member cannot act on.
    math_tools = _math()
    hostile: list[tuple[str, dict]] = [
        ("percentage_change", {"start": 0, "end": 0}),
        ("compound_growth", {"start": 0, "rate": -1, "periods": 0}),
        (
            "break_even_units",
            {"fixed_costs": 0, "price_per_unit": 0, "variable_cost_per_unit": 0},
        ),
        ("payback_period", {"initial_investment": 0, "cash_flow_per_period": 0}),
        (
            "ratio",
            {"numerator": 0, "denominator": 0, "numerator_unit": "", "denominator_unit": ""},
        ),
    ]
    for name, kwargs in hostile:
        result = getattr(math_tools, name)(**kwargs)
        assert isinstance(result, dict), name
