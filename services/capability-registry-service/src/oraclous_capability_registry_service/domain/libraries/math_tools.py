"""Curated ``math-tools`` library (#822) — pure, deterministic, stdlib-only arithmetic.

Every figure in a deliverable is written by a language model in prose today, and nothing in a run
ever recomputes it: a unit-economics model, a break-even, a payback period all rest on a model's
arithmetic. These five functions are that arithmetic, computed instead of written, so the same
inputs give the same number every time.

Four properties hold for every function here, and each is a rule for the next one added:

**Exact.** No rounding, no formatting, no clock, no randomness — the value is whatever the
arithmetic gives. ``percentage_change`` subtracts before it divides on purpose: ``(end - start) /
start * 100`` gives 30.0 where ``(end / start - 1) * 100`` gives 30.000000000000004, and the round
number is the one a reader expects to see in a document.

**Total.** Bad input returns ``{"error": <code>, "detail": ...}``; nothing here raises. An undefined
calculation is not a tool failure — the call worked, and the member that made it needs to read
"this product never breaks even" and say so, rather than seeing an opaque tool error.

**Bounded.** :data:`MAX_PERIODS` caps the period count. :class:`LibraryGroupExecutor` dispatches in
a thread it cannot kill and its only other guard caps STRING length, so a nine-digit exponent
reachable through a keyless, in-process, curated operation is an unbounded CPU and memory cost. The
cap sits far above real work (a thirty-year monthly model is 360 periods) and refuses the rest as a
typed error like any other bad input.

**Pure.** No I/O, no state — same arguments, same dict, forever.

Mounted as the ``math-tools`` tool group by :mod:`registry`, separately from ``text-tools``: a
member binds a group and gets every operation in it, so the member counting words must not also be
handed a compounding function.
"""

from __future__ import annotations

import math

#: Upper bound on a period count. 10,000 periods of compounding is still cheap arithmetic and a
#: thirty-year monthly model is 360, so the cap is drawn an order of magnitude above real work — it
#: exists to refuse an exponent that would burn the dispatch thread, not to constrain a model.
MAX_PERIODS = 100_000


def _error(code: str, detail: str) -> dict:
    """The typed-error result shape every operation here returns instead of raising."""
    return {"error": code, "detail": detail}


def _valid_periods(periods: object) -> bool:
    """A period count is a whole number, not negative, and within :data:`MAX_PERIODS`.

    A fractional count is refused rather than computed: ``1.08 ** 2.5`` is arithmetically fine,
    which is exactly the problem — it answers a question nobody asked with a confident figure.
    """
    if isinstance(periods, bool) or not isinstance(periods, int):
        return False
    return 0 <= periods <= MAX_PERIODS


def percentage_change(start: float, end: float) -> dict:
    """The signed percentage change from ``start`` to ``end``.

    A fall is negative, not an absolute magnitude — the sign is the part prose routinely loses.
    Growth from a zero base has no percentage, so it is an ``undefined_base`` error.
    """
    if start == 0:
        return _error(
            "undefined_base",
            "percentage change from a zero base is undefined: there is nothing to grow from",
        )
    return {"percent": (end - start) / start * 100, "start": start, "end": end}


def compound_growth(start: float, rate: float, periods: int) -> dict:
    """``start`` grown by ``rate`` per period, compounded over ``periods`` periods.

    Compounded, not multiplied: 100 grown 50% twice is 225, not 200. A negative rate is decay,
    which is the same arithmetic and the common shape of churn and price erosion.
    """
    if not _valid_periods(periods):
        return _error(
            "periods_out_of_range",
            f"'periods' must be a whole number between 0 and {MAX_PERIODS}, got {periods!r}",
        )
    try:
        value = start * (1 + rate) ** periods
    except OverflowError:
        return _error(
            "value_out_of_range",
            "the compounded value is too large to represent; reduce the rate or the period count",
        )
    return {"value": value, "start": start, "rate": rate, "periods": periods}


def break_even_units(
    fixed_costs: float, price_per_unit: float, variable_cost_per_unit: float
) -> dict:
    """The unit volume at which contribution covers the fixed costs.

    Both the exact figure and the whole one are returned deliberately: the exact value is the
    arithmetic, and the whole number is what a decision needs. Leaving the rounding to the caller
    is how 666.67 becomes a reported break-even of 666, which does not break even.
    """
    if fixed_costs < 0:
        return _error("invalid_fixed_costs", f"'fixed_costs' cannot be negative, got {fixed_costs}")
    contribution = price_per_unit - variable_cost_per_unit
    if contribution <= 0:
        return _error(
            "no_contribution",
            "price is at or below variable cost, so every unit loses money and there is no "
            "break-even volume",
        )
    units = fixed_costs / contribution
    return {
        "units": units,
        "units_whole": math.ceil(units),
        "contribution_per_unit": contribution,
    }


def payback_period(initial_investment: float, cash_flow_per_period: float) -> dict:
    """How many periods of ``cash_flow_per_period`` it takes to recover the investment.

    A zero or negative periodic cash flow never recovers it, and "never" is not a number — which
    is the case a model is most likely to paper over, so it is a typed error.
    """
    if initial_investment < 0:
        return _error(
            "invalid_investment",
            f"'initial_investment' cannot be negative, got {initial_investment}",
        )
    if cash_flow_per_period <= 0:
        return _error(
            "no_payback",
            "a cash flow at or below zero never recovers the investment",
        )
    return {
        "periods": initial_investment / cash_flow_per_period,
        "initial_investment": initial_investment,
        "cash_flow_per_period": cash_flow_per_period,
    }


def ratio(numerator: float, denominator: float, numerator_unit: str, denominator_unit: str) -> dict:
    """``numerator / denominator``, carrying its units into the result.

    The units are the point. A bare 150.0 is the kind of figure that gets relabelled on the way
    into a document; "USD per customer" travels with the number that earned it.
    """
    if denominator == 0:
        return _error("division_by_zero", "'denominator' is zero, so the ratio is undefined")
    return {
        "value": numerator / denominator,
        "unit": f"{numerator_unit} per {denominator_unit}",
        "numerator": numerator,
        "denominator": denominator,
    }
