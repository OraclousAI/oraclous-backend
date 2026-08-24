"""Math-tools DEPLOYED-STACK proof through the API GATEWAY — NO fakes (#822).

A real user, through the gateway (:8006), discovers the seeded **Math Tools** library tool,
instantiates it, and dispatches the curated arithmetic — which the registry runs in-process and
whose dict output lands on the org-scoped Execution row (readable through the gateway). The three
things a unit test cannot prove about the deployed stack are proven here: the new group is SEEDED
and separate from Text Tools, a whole number survives the wire as a number, and an undefined
calculation comes back as data on a SUCCESSFUL execution rather than as a tool failure. Real
capability-registry; nothing mocked, no internal port, no DB-direct (rule 5). The package
auto-skips when the gateway is down (conftest) — a skip is not a pass.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _capabilities(c: httpx.Client) -> dict:
    return {x["name"]: x for x in c.get("/api/v1/capabilities").json()["capabilities"]}


def _math_tools_cap(c: httpx.Client) -> dict:
    by_name = _capabilities(c)
    assert "Math Tools" in by_name, f"math-tools not seeded; got {sorted(by_name)}"
    return by_name["Math Tools"]


def _instantiate(c: httpx.Client, cap_id: str) -> str:
    inst = c.post(
        "/api/v1/instances",
        json={"capability_id": cap_id, "name": "math-tools", "configuration": {}, "settings": {}},
    )
    assert inst.status_code == 201, inst.text
    return inst.json()["id"]


def _run(c: httpx.Client, iid: str, payload: dict) -> dict:
    ex = c.post(f"/api/v1/instances/{iid}/execute", json={"input_data": payload})
    assert ex.status_code == 201, ex.text
    return ex.json()


def test_the_curated_arithmetic_runs_and_lands_on_the_execution_row(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """THE PROOF: each curated operation dispatches in-process; its output persists on the org row.

    The compound-growth call is the issue's own worked example — 40,000 a month at 8% a month for
    18 months, where a model writing prose says "roughly 151,000" and the real figure is nearly
    9,000 higher. Note the round 40000: a whole number where a decimal is declared, which is what
    a member actually sends and what the dispatcher had to be widened to accept.
    """
    user = register(f"mathtools{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    cap = _math_tools_cap(c)
    iid = _instantiate(c, cap["id"])

    growth = _run(
        c, iid, {"operation": "compound_growth", "start": 40000, "rate": 0.08, "periods": 18}
    )
    assert growth["status"] == "SUCCESS", growth
    assert growth["output_data"]["value"] == pytest.approx(159840.77996739736, rel=1e-9)

    pct = _run(c, iid, {"operation": "percentage_change", "start": 40000, "end": 52000})
    assert pct["status"] == "SUCCESS" and pct["output_data"]["percent"] == 30.0

    be = _run(
        c,
        iid,
        {
            "operation": "break_even_units",
            "fixed_costs": 50000,
            "price_per_unit": 120,
            "variable_cost_per_unit": 45,
        },
    )
    assert be["status"] == "SUCCESS" and be["output_data"]["units_whole"] == 667

    pb = _run(
        c,
        iid,
        {
            "operation": "payback_period",
            "initial_investment": 250000,
            "cash_flow_per_period": 40000,
        },
    )
    assert pb["status"] == "SUCCESS" and pb["output_data"]["periods"] == 6.25

    r = _run(
        c,
        iid,
        {
            "operation": "ratio",
            "numerator": 180000,
            "denominator": 1200,
            "numerator_unit": "USD",
            "denominator_unit": "customer",
        },
    )
    assert r["status"] == "SUCCESS" and r["output_data"]["unit"] == "USD per customer"

    # the output persisted on the org-scoped Execution row, read back THROUGH THE GATEWAY
    got = c.get(f"/api/v1/executions/{pct['id']}")
    assert got.status_code == 200 and got.json()["output_data"]["percent"] == 30.0


def test_an_undefined_calculation_comes_back_as_data_not_as_a_tool_failure(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """A product priced below variable cost never breaks even. The call still WORKED, so the
    execution succeeds and the member reads a named reason it can write down — rather than an
    opaque tool failure it can only retry."""
    user = register(f"mathundefined{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    cap = _math_tools_cap(c)
    iid = _instantiate(c, cap["id"])

    out = _run(
        c,
        iid,
        {
            "operation": "break_even_units",
            "fixed_costs": 50000,
            "price_per_unit": 40,
            "variable_cost_per_unit": 45,
        },
    )
    assert out["status"] == "SUCCESS", out
    assert out["output_data"]["error"] == "no_contribution"


def test_the_groups_are_separate_and_the_period_count_is_bounded(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Two properties the deployed catalogue has to carry, not just the unit suite.

    A member binds a GROUP and gets every operation in it, so a text operation must be unknown on
    the math tool and vice versa. And the period count is bounded in the curated function, because
    dispatch runs in a thread the platform cannot kill and its only other guard caps string length.
    """
    user = register(f"mathgroups{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    by_name = _capabilities(c)
    assert "Text Tools" in by_name, f"text-tools not seeded; got {sorted(by_name)}"
    math_iid = _instantiate(c, _math_tools_cap(c)["id"])

    leaked = _run(c, math_iid, {"operation": "word_count", "text": "a b"})
    assert leaked["status"] == "FAILED" and leaked["error_type"] == "INVALID_OPERATION"

    bounded = _run(
        c, math_iid, {"operation": "compound_growth", "start": 2, "rate": 1.0, "periods": 10**9}
    )
    assert bounded["status"] == "SUCCESS"
    assert bounded["output_data"]["error"] == "periods_out_of_range"
