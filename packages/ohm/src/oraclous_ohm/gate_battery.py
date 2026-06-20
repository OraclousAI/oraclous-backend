"""Named gate battery runner (ADR-037 Decision 2 — E4 #470).

A pure runner (injected-dispatch like ``run_team``): given a resolved ``OHMGateBattery`` + the
graded output + an injected ``core/evaluate`` invoker, it runs ``deterministic`` checks in-process
(a ``core/check/<id>`` predicate, no LLM) and ``evaluator`` checks via the judge, then reduces to an
``OHMBatteryVerdict`` by the battery's floor:

* ``floor="and"`` (EURail 10-gate): PASS iff every *applicable* check passes — flat AND.
* ``floor="precedence"`` (book QA Lock): PASS iff no CRITICAL check fails; MAJOR/MINOR failures are
  reported-but-non-blocking while every CRITICAL clears.

Fail-closed throughout: an unknown ``check_ref``, a predicate error, or a below-threshold evaluator
all FAIL the check (never silently pass). ``applies_when`` skips a check fail-closed (e.g. EURail's
refresh-only gates 9-10 in fresh mode) — a skipped check never blocks.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from oraclous_ohm.manifest import OHMGateBattery, OHMGateCheck, OHMManifest, OHMRunIf

# Grade one evaluator check's `rubric` against the output → a score in [0, 1] (the caller wires this
# to core/evaluate). Kept injected so packages/ohm needs no dependency on packages/eval.
EvaluatorInvoke = Callable[[OHMGateCheck, str], Awaitable[float]]
# A deterministic core/check predicate: (output, params) → (passed, reason-label).
Predicate = Callable[[str, dict[str, Any]], tuple[bool, str]]

BATTERY_PREFIX = "battery:"


class OHMCheckVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    severity: Literal["CRITICAL", "MAJOR", "MINOR"]
    reason: str  # a label/disposition (needs-source / below-threshold / …) — never customer text
    score: float | None = None  # set only for evaluator checks
    skipped: bool = False


class OHMBatteryVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    check_verdicts: list[OHMCheckVerdict] = Field(default_factory=list)
    failures: list[OHMCheckVerdict] = Field(default_factory=list)  # the blocking subset
    blocking_severity: Literal["CRITICAL", "MAJOR", "MINOR"] | None = None
    recommended_action: Literal["pass", "block", "escalate_human"]


class UnknownBattery(Exception):
    """``success_criteria``/``convergence`` references a ``battery:<name>`` not declared on the
    manifest (fail-closed — the load/run aborts rather than silently skip the gate)."""


# ── deterministic core/check predicates (the initial set #470 ships; extensible) ──────────────


def _check_min_length(output: str, params: dict[str, Any]) -> tuple[bool, str]:
    n = int(params.get("min", 1))
    return (len(output) >= n, f"length {len(output)} (min {n})")


def _check_contains_all(output: str, params: dict[str, Any]) -> tuple[bool, str]:
    terms = [str(t) for t in params.get("terms", [])]
    missing = [t for t in terms if t.lower() not in output.lower()]
    return (not missing, "all present" if not missing else f"missing {missing}")


def _check_no_forbidden(output: str, params: dict[str, Any]) -> tuple[bool, str]:
    """No disputed/needs-source markers remain (book QA-Lock integrity)."""
    terms = [str(t) for t in params.get("terms", ["DISPUTED", "needs-source", "TODO"])]
    found = [t for t in terms if t.lower() in output.lower()]
    return (not found, "clean" if not found else f"found {found}")


def _check_min_records(output: str, params: dict[str, Any]) -> tuple[bool, str]:
    """At least N records — JSON array length, else non-blank line count (EURail ledger floor)."""
    n = int(params.get("min", 1))
    try:
        parsed = json.loads(output)
        count = len(parsed) if isinstance(parsed, list) else 1
    except (json.JSONDecodeError, TypeError):
        count = len([ln for ln in output.splitlines() if ln.strip()])
    return (count >= n, f"{count} records (min {n})")


def _check_citation_coverage(output: str, params: dict[str, Any]) -> tuple[bool, str]:
    """Fraction of sentences carrying a citation marker (``[...]`` / ``(source...``) ≥ min_ratio."""
    min_ratio = float(params.get("min_ratio", 0.333))
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", output) if s.strip()]
    if not sentences:
        return (False, "no sentences")
    cited = sum(1 for s in sentences if re.search(r"\[[^\]]+\]|\(source", s, re.IGNORECASE))
    ratio = cited / len(sentences)
    return (ratio >= min_ratio, f"citation ratio {ratio:.2f} (min {min_ratio})")


def _check_json_valid(output: str, _params: dict[str, Any]) -> tuple[bool, str]:
    try:
        json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return (False, "not valid JSON")
    return (True, "valid JSON")


CORE_CHECKS: dict[str, Predicate] = {
    "core/check/min-length": _check_min_length,
    "core/check/contains-all": _check_contains_all,
    "core/check/no-forbidden": _check_no_forbidden,
    "core/check/min-records": _check_min_records,
    "core/check/citation-coverage": _check_citation_coverage,
    "core/check/json-valid": _check_json_valid,
}

_SEVERITY_RANK = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}


def resolve_battery(manifest: OHMManifest, reference: str) -> OHMGateBattery:
    """Resolve a ``battery:<name>`` reference against ``manifest.batteries`` (fail-closed)."""
    name = reference[len(BATTERY_PREFIX) :] if reference.startswith(BATTERY_PREFIX) else reference
    battery = manifest.battery_by_name(name)
    if battery is None:
        raise UnknownBattery(f"battery {name!r} is not declared on the manifest")
    return battery


def is_battery_reference(value: str | None) -> bool:
    return bool(value) and value.startswith(BATTERY_PREFIX)  # type: ignore[union-attr]


def _applies(run_if: OHMRunIf, context: dict[str, Any]) -> bool:
    """Evaluate an ``applies_when`` against an injected run context (e.g. {"mode": "refresh"}).
    Fail-closed: a missing source or any error means the check does NOT apply (it is skipped)."""
    try:
        source = context.get(run_if.from_role)
        value = source.get(run_if.field) if (run_if.field and isinstance(source, dict)) else source
        target = run_if.value
        op = run_if.op
        if op == "truthy":
            return bool(value)
        if op == "eq":
            return value == target
        if op == "ne":
            return value != target
        if op == "in":
            return value in target  # type: ignore[operator]
        if value is None or target is None:
            return False
        return {
            "gt": value > target,
            "lt": value < target,
            "gte": value >= target,
            "lte": value <= target,
        }[op]
    except Exception:  # noqa: BLE001 — fail-closed: unevaluable applies_when → does not apply
        return False


async def evaluate_gate(
    manifest: OHMManifest,
    output: str,
    *,
    evaluate: EvaluatorInvoke,
    gate: Literal["success_criteria", "convergence"] = "success_criteria",
    context: dict[str, Any] | None = None,
) -> OHMBatteryVerdict | None:
    """The manifest-gate entry point the harness calls (ADR-037 #470): if the manifest's
    ``success_criteria`` (or ``termination.convergence``) is a ``battery:<name>`` reference, resolve
    it (fail-closed) and run it over ``output``, returning the verdict. Returns ``None`` when the
    gate is prose (that is ``core/evaluate``'s path, Decision 1) or unset — so the caller routes."""
    orch = manifest.orchestration
    if orch is None:
        return None
    reference = (
        orch.success_criteria
        if gate == "success_criteria"
        else (orch.termination.convergence or "")
    )
    if not is_battery_reference(reference):
        return None
    battery = resolve_battery(manifest, reference)
    return await run_battery(battery, output, evaluate=evaluate, context=context)


async def run_battery(
    battery: OHMGateBattery,
    output: str,
    *,
    evaluate: EvaluatorInvoke,
    context: dict[str, Any] | None = None,
    predicates: dict[str, Predicate] | None = None,
) -> OHMBatteryVerdict:
    ctx = context or {}
    preds = predicates or CORE_CHECKS
    verdicts: list[OHMCheckVerdict] = []

    for check in battery.checks:
        if check.applies_when is not None and not _applies(check.applies_when, ctx):
            verdicts.append(
                OHMCheckVerdict(
                    name=check.name,
                    passed=True,
                    severity=check.severity,
                    reason="skipped (applies_when)",
                    skipped=True,
                )
            )
            continue
        if check.kind == "evaluator":
            score = await evaluate(check, output)
            threshold = float(check.params.get("threshold", 0.5))
            passed = score >= threshold
            verdicts.append(
                OHMCheckVerdict(
                    name=check.name,
                    passed=passed,
                    severity=check.severity,
                    reason="ok" if passed else f"below threshold ({threshold})",
                    score=round(float(score), 4),
                )
            )
        else:  # deterministic
            predicate = preds.get(check.check_ref or "")
            if predicate is None:
                verdicts.append(
                    OHMCheckVerdict(
                        name=check.name,
                        passed=False,  # fail-closed on an unknown predicate
                        severity=check.severity,
                        reason=f"unknown check_ref {check.check_ref!r}",
                    )
                )
                continue
            try:
                passed, reason = predicate(output, check.params)
            except Exception:  # noqa: BLE001 — a predicate error fails the check, never crashes
                passed, reason = False, "check error"
            verdicts.append(
                OHMCheckVerdict(
                    name=check.name, passed=passed, severity=check.severity, reason=reason
                )
            )

    return _reduce(battery, verdicts)


def _reduce(battery: OHMGateBattery, verdicts: list[OHMCheckVerdict]) -> OHMBatteryVerdict:
    fails = [v for v in verdicts if not v.passed and not v.skipped]
    if battery.floor == "and":
        blocking = fails  # flat AND — any applicable failure blocks
    else:  # precedence — only CRITICAL failures block; MAJOR/MINOR are reported, non-blocking
        blocking = [v for v in fails if v.severity == "CRITICAL"]
    passed = not blocking
    blocking_severity = (
        max(blocking, key=lambda v: _SEVERITY_RANK[v.severity]).severity if blocking else None
    )
    if any(v.severity == "CRITICAL" for v in blocking):
        action: Literal["pass", "block", "escalate_human"] = "escalate_human"
    elif not passed:
        action = "block"
    else:
        action = "pass"
    return OHMBatteryVerdict(
        passed=passed,
        check_verdicts=verdicts,
        failures=blocking,
        blocking_severity=blocking_severity,
        recommended_action=action,
    )
