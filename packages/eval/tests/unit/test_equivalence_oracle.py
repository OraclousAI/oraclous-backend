"""#597 (ADR-047 §7) — Layer-2: the parameterized EURail-ledger equivalence oracle.

Fixture (synthetic) ledgers prove the oracle's shape: band-relative equivalence (within the imported
team's band, not exactly N), the 10-gate report-editor battery (floor='and'), and fail-closed on a
short ledger / an unresolved marker / a low editorial score. The reviewer runs the REAL EURail
equivalence (the ledger + imported team live on the reviewer's machine).
"""

from __future__ import annotations

import json

import pytest
from oraclous_eval.equivalence import (
    BaselineBand,
    build_report_editor_battery,
    count_ledger_records,
    ledger_equivalence,
)
from oraclous_ohm.gate_battery import OHMGateCheck

pytestmark = pytest.mark.unit


def _ledger(n: int, *, marker: str = "") -> str:
    rows = [{"id": i, "route": f"R{i}", "fare": 10 + i} for i in range(n)]
    if marker:
        rows.append({"id": "x", "route": marker, "fare": 0})
    return json.dumps(rows)


def _fake_eval(score: float):
    async def _eval(check: OHMGateCheck, output: str) -> float:
        return score

    return _eval


# the imported team reproduced 880 of the shipped 909; the compiled team may land within ±10%.
_BAND = BaselineBand(reference_records=909, baseline_records=880, tolerance=0.1)


def test_baseline_band_is_relative_not_exact() -> None:
    assert _BAND.lo == 792 and _BAND.hi == 909  # ceil(880*1.1)=968 clamped to the reference 909
    assert _BAND.contains(880) and _BAND.contains(792) and _BAND.contains(909)
    assert not _BAND.contains(700)  # below the imported team's band
    assert not _BAND.contains(950)  # above the ground-truth ledger — hallucinated records


def test_the_report_editor_battery_is_ten_gates_floor_and() -> None:
    battery = build_report_editor_battery(min_records=792)
    assert battery.floor == "and"
    assert len(battery.checks) == 10
    assert sum(1 for c in battery.checks if c.kind == "deterministic") == 6
    assert sum(1 for c in battery.checks if c.kind == "evaluator") == 4


async def test_a_within_band_clean_ledger_passes() -> None:
    battery = build_report_editor_battery(min_records=792, required_fields=["id", "route"])
    verdict = await ledger_equivalence(
        _ledger(880), baseline_band=_BAND, battery=battery, evaluate=_fake_eval(0.95)
    )
    assert verdict.passed is True
    assert verdict.within_band is True
    assert verdict.battery.passed is True
    assert verdict.reasons == []


async def test_a_below_band_ledger_fails_equivalence() -> None:
    battery = build_report_editor_battery(min_records=792)
    verdict = await ledger_equivalence(
        _ledger(500), baseline_band=_BAND, battery=battery, evaluate=_fake_eval(0.95)
    )
    assert verdict.passed is False
    assert verdict.within_band is False
    # the deterministic record-floor gate ALSO fails (500 < 792) — the band + battery agree.
    assert any("record count 500" in r for r in verdict.reasons)


async def test_an_over_ground_truth_ledger_fails_equivalence() -> None:
    # more records than the shipped 909 → hallucinated, outside the band even though it is "more".
    verdict = await ledger_equivalence(
        _ledger(1000),
        baseline_band=_BAND,
        battery=build_report_editor_battery(min_records=792),
        evaluate=_fake_eval(0.95),
    )
    assert verdict.passed is False
    assert verdict.within_band is False


async def test_an_unresolved_marker_fails_the_critical_gate() -> None:
    battery = build_report_editor_battery(min_records=792, forbidden_markers=["DISPUTED"])
    verdict = await ledger_equivalence(
        _ledger(880, marker="DISPUTED"),
        baseline_band=_BAND,
        battery=battery,
        evaluate=_fake_eval(0.95),
    )
    assert verdict.passed is False
    assert verdict.within_band is True  # the count is fine — the BATTERY blocks it
    assert any("no-unresolved-markers" in r for r in verdict.reasons)


async def test_a_low_editorial_score_fails_an_evaluator_gate() -> None:
    battery = build_report_editor_battery(min_records=792, evaluator_threshold=0.7)
    verdict = await ledger_equivalence(
        _ledger(880), baseline_band=_BAND, battery=battery, evaluate=_fake_eval(0.3)
    )
    assert verdict.passed is False
    assert verdict.within_band is True
    # floor='and' → every evaluator gate below threshold blocks
    assert not verdict.battery.passed


def test_count_ledger_records_json_and_lines() -> None:
    assert count_ledger_records(_ledger(42)) == 42
    assert count_ledger_records("a\n\nb\nc") == 3  # non-blank lines when not a JSON array
