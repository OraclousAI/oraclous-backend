"""Named gate battery runner (ADR-037 / E4 #470): both floor modes (EURail flat-AND, book QA-Lock
precedence), the deterministic core/check predicates, fail-closed paths, applies_when skip, and the
battery:<name> resolution. No network — the evaluator invoker is a fake."""

from __future__ import annotations

import pytest
from oraclous_ohm.gate_battery import (
    BATTERY_PREFIX,
    UnknownBattery,
    evaluate_gate,
    is_battery_reference,
    resolve_battery,
    run_battery,
)
from oraclous_ohm.manifest import (
    OHMGateBattery,
    OHMGateCheck,
    OHMManifest,
    OHMMetadata,
    OHMOrchestration,
    OHMRunIf,
    OHMRuntime,
)

pytestmark = [pytest.mark.unit]


def _det(name: str, check_ref: str, severity: str = "CRITICAL", **params) -> OHMGateCheck:
    return OHMGateCheck(
        name=name, kind="deterministic", check_ref=check_ref, severity=severity, params=params
    )


def _evalc(name: str, severity: str = "CRITICAL", threshold: float = 0.6) -> OHMGateCheck:
    return OHMGateCheck(
        name=name,
        kind="evaluator",
        rubric=f"is {name} good",
        severity=severity,
        params={"threshold": threshold},
    )


async def _fake_eval(score: float):
    async def _e(check: OHMGateCheck, output: str) -> float:
        return score

    return _e


# ── floor: AND (EURail 10-gate) ───────────────────────────────────────────────────────────────


async def test_and_floor_all_pass() -> None:
    battery = OHMGateBattery(
        name="b",
        floor="and",
        checks=[
            _det("len", "core/check/min-length", min=3),
            _det("clean", "core/check/no-forbidden"),
        ],
    )
    v = await run_battery(battery, "a clean output", evaluate=await _fake_eval(1.0))
    assert v.passed is True and v.recommended_action == "pass" and v.failures == []


async def test_and_floor_any_fail_blocks() -> None:
    battery = OHMGateBattery(
        name="b",
        floor="and",
        checks=[_det("clean", "core/check/no-forbidden", severity="MAJOR")],
    )
    v = await run_battery(battery, "this has a DISPUTED claim", evaluate=await _fake_eval(1.0))
    assert v.passed is False  # flat AND — even a MAJOR failure blocks
    assert v.recommended_action == "block" and v.blocking_severity == "MAJOR"


# ── floor: PRECEDENCE (book QA Lock) ──────────────────────────────────────────────────────────


async def test_precedence_major_failure_is_non_blocking() -> None:
    battery = OHMGateBattery(
        name="qa",
        floor="precedence",
        checks=[
            _det("integrity", "core/check/no-forbidden", severity="CRITICAL"),  # passes (clean)
            _det("grammar", "core/check/min-length", severity="MINOR", min=10_000),  # fails (short)
        ],
    )
    v = await run_battery(battery, "clean prose", evaluate=await _fake_eval(1.0))
    assert v.passed is True  # MINOR fails but no CRITICAL → chapter can lock
    assert any(not c.passed for c in v.check_verdicts)  # the MINOR failure is still REPORTED


async def test_precedence_critical_failure_escalates() -> None:
    battery = OHMGateBattery(
        name="qa",
        floor="precedence",
        checks=[_det("integrity", "core/check/no-forbidden", severity="CRITICAL")],
    )
    v = await run_battery(battery, "contains needs-source marker", evaluate=await _fake_eval(1.0))
    assert v.passed is False and v.recommended_action == "escalate_human"
    assert v.blocking_severity == "CRITICAL"


# ── deterministic predicates + evaluator checks ───────────────────────────────────────────────


async def test_evaluator_check_uses_threshold() -> None:
    battery = OHMGateBattery(name="b", floor="and", checks=[_evalc("depth", threshold=0.6)])
    assert (await run_battery(battery, "x", evaluate=await _fake_eval(0.8))).passed is True
    assert (await run_battery(battery, "x", evaluate=await _fake_eval(0.4))).passed is False


@pytest.mark.parametrize(
    "ref,params,output,expected",
    [
        ("core/check/min-records", {"min": 3}, '["a","b","c","d"]', True),
        ("core/check/min-records", {"min": 5}, "line1\nline2", False),
        ("core/check/citation-coverage", {"min_ratio": 0.5}, "A claim [1]. Another [2].", True),
        ("core/check/citation-coverage", {"min_ratio": 0.5}, "A claim. No source here.", False),
        ("core/check/json-valid", {}, '{"ok": true}', True),
        ("core/check/json-valid", {}, "not json", False),
        ("core/check/contains-all", {"terms": ["paris"]}, "in Paris today", True),
    ],
)
async def test_core_check_predicates(ref, params, output, expected) -> None:
    battery = OHMGateBattery(name="b", floor="and", checks=[_det("c", ref, **params)])
    assert (await run_battery(battery, output, evaluate=await _fake_eval(1.0))).passed is expected


async def test_unknown_check_ref_fails_closed() -> None:
    battery = OHMGateBattery(name="b", floor="and", checks=[_det("c", "core/check/does-not-exist")])
    v = await run_battery(battery, "x", evaluate=await _fake_eval(1.0))
    assert v.passed is False and "unknown check_ref" in v.failures[0].reason


# ── applies_when (refresh-only gates) + resolution ────────────────────────────────────────────


async def test_applies_when_skips_fail_closed() -> None:
    refresh_only = OHMGateCheck(
        name="ledger-diff",
        kind="deterministic",
        check_ref="core/check/min-records",
        params={"min": 999},
        applies_when=OHMRunIf(from_role="mode", op="eq", value="refresh"),
    )
    battery = OHMGateBattery(name="b", floor="and", checks=[refresh_only])
    # fresh mode → the gate does not apply → skipped → does not block
    fresh = await run_battery(
        battery, "short", evaluate=await _fake_eval(1.0), context={"mode": "fresh"}
    )
    assert fresh.passed is True and fresh.check_verdicts[0].skipped is True
    # refresh mode → the gate applies → runs → fails (too few records)
    refr = await run_battery(
        battery, "short", evaluate=await _fake_eval(1.0), context={"mode": "refresh"}
    )
    assert refr.passed is False


def _team_manifest(batteries: dict) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(
            id="00000000-0000-0000-0000-000000000001",
            name="t",
            kind="team",
            owner_organization_id="00000000-0000-0000-0000-000000000002",
        ),
        runtime=OHMRuntime(entrypoint="primary"),
        batteries=batteries,
    )


async def test_evaluate_gate_routes_battery_vs_prose() -> None:
    battery = OHMGateBattery(
        name="qa", floor="and", checks=[_det("len", "core/check/min-length", min=1)]
    )
    m = _team_manifest({"qa": battery})
    m.orchestration = OHMOrchestration(success_criteria="battery:qa")
    v = await evaluate_gate(m, "some output", evaluate=await _fake_eval(1.0))
    assert v is not None and v.passed is True  # battery: ref → runs the battery

    m.orchestration = OHMOrchestration(success_criteria="the answer is factually correct")
    # prose criterion → None (that is core/evaluate's path, not a battery)
    assert await evaluate_gate(m, "x", evaluate=await _fake_eval(1.0)) is None


def test_resolve_battery_and_reference_guard() -> None:
    battery = OHMGateBattery(name="qa", floor="precedence", checks=[_evalc("x")])
    manifest = _team_manifest({"qa": battery})
    assert is_battery_reference("battery:qa") is True
    assert is_battery_reference("a prose criterion") is False
    assert resolve_battery(manifest, "battery:qa").name == "qa"
    assert resolve_battery(manifest, "qa").name == "qa"  # bare name also resolves
    with pytest.raises(UnknownBattery):
        resolve_battery(manifest, f"{BATTERY_PREFIX}missing")


def test_manifest_parses_batteries_block() -> None:
    raw = {
        "ohm_version": "1.1",
        "metadata": {
            "id": "00000000-0000-0000-0000-000000000001",
            "name": "t",
            "kind": "team",
            "owner_organization_id": "00000000-0000-0000-0000-000000000002",
        },
        "runtime": {"entrypoint": "primary"},
        "orchestration": {"success_criteria": "battery:report-editor-10gate"},
        "batteries": {
            "report-editor-10gate": {
                "name": "report-editor-10gate",
                "floor": "and",
                "checks": [
                    {
                        "name": "records",
                        "kind": "deterministic",
                        "check_ref": "core/check/min-records",
                        "params": {"min": 600},
                    }
                ],
            }
        },
    }
    m = OHMManifest.model_validate(raw)
    assert m.battery_by_name("report-editor-10gate").floor == "and"
    assert m.orchestration.success_criteria == "battery:report-editor-10gate"


def test_gate_check_kind_requires_its_target() -> None:  # #479
    """An evaluator check MUST carry a non-empty rubric (else it grades '' → core/evaluate 422s and
    collapses the whole battery); a deterministic check MUST name a check_ref. Caught at load."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        OHMGateCheck(name="c", kind="evaluator")  # no rubric
    with pytest.raises(ValidationError):
        OHMGateCheck(name="c", kind="evaluator", rubric="   ")  # blank rubric
    with pytest.raises(ValidationError):
        OHMGateCheck(name="c", kind="deterministic")  # no check_ref
    # the well-formed cases parse
    assert OHMGateCheck(name="c", kind="evaluator", rubric="grade it").rubric == "grade it"
    assert (
        OHMGateCheck(name="c", kind="deterministic", check_ref="core/check/x").kind
        == "deterministic"
    )
