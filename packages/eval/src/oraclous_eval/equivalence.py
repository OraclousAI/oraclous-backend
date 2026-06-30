"""#597 (ADR-047 §7) — Layer-2: the EURail-ledger equivalence oracle (PARAMETERIZED).

The only hard objective oracle (binds EURail): give the compiler EURail's objective in prose, run
the generated team through the gateway, and check the run reproduces the shipped reference ledger
**about as well as the IMPORTED team does** AND passes the 10-gate report-editor battery
(``floor="and"``). The equivalence is band-relative, NOT "exactly N": the compiled team must land
within the IMPORTED (known-good) team's own recorded band, never an absolute count.

Per the CTO's #597 split this module is the parameterized ORACLE CODE + its fixture tests — it takes
the objective / reference ledger / imported-team baseline as INPUTS. **The reviewer runs the actual
EURail equivalence in RULE-4 review** (the EURail ledger + imported ``agent-pack`` live only on the
reviewer's machine); here we ship the metric, the baseline-band logic, the 10-gate battery builder,
and synthetic-ledger unit tests that prove the oracle's shape.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Literal

from oraclous_ohm.gate_battery import (
    EvaluatorInvoke,
    OHMBatteryVerdict,
    Predicate,
    run_battery,
)
from oraclous_ohm.manifest import OHMGateBattery, OHMGateCheck
from pydantic import BaseModel, ConfigDict, Field


def count_ledger_records(deliverable: str) -> int:
    """Records in a deliverable ledger — a JSON array's length, else the non-blank line count (the
    same floor ``core/check/min-records`` uses, so the oracle and the battery agree)."""
    try:
        parsed = json.loads(deliverable)
    except (json.JSONDecodeError, TypeError):
        return len([ln for ln in deliverable.splitlines() if ln.strip()])
    if isinstance(parsed, list):
        return len(parsed)
    return 1


class BaselineBand(BaseModel):
    """The IMPORTED (known-good) team's own recorded ledger band. The compiled team is equivalent
    when its record count lands WITHIN this band — not when it hits the reference exactly. Recorded
    once from the imported team's run (the reviewer supplies the real EURail numbers)."""

    model_config = ConfigDict(extra="forbid")

    reference_records: int = Field(ge=0)  # ground-truth ledger size (e.g. EURail's shipped 909)
    baseline_records: int = Field(ge=0)  # what the IMPORTED team itself reproduced (the centre)
    tolerance: float = Field(default=0.1, ge=0.0, le=1.0)  # ± fraction of the baseline allowed

    @property
    def lo(self) -> int:
        return max(0, math.floor(self.baseline_records * (1.0 - self.tolerance)))

    @property
    def hi(self) -> int:
        # never credit MORE than the ground truth — extra records are hallucinated, not equivalence.
        return min(
            self.reference_records, math.ceil(self.baseline_records * (1.0 + self.tolerance))
        )

    def contains(self, record_count: int) -> bool:
        return self.lo <= record_count <= self.hi


class EquivalenceVerdict(BaseModel):
    """The Layer-2 oracle verdict: PASS iff the deliverable's record count is within the imported
    team's band AND the 10-gate report-editor battery passes (``floor="and"`` — every gate)."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    record_count: int
    within_band: bool
    band_lo: int
    band_hi: int
    battery: OHMBatteryVerdict
    reasons: list[str] = Field(default_factory=list)


_Severity = Literal["CRITICAL", "MAJOR", "MINOR"]

# the default report-editor quality rubrics (name, rubric, severity) — the evaluator gates. The
# reviewer swaps in EURail's real editorial rubrics; these are a representative scaffold.
_DEFAULT_QUALITY_RUBRICS: tuple[tuple[str, str, _Severity], ...] = (
    (
        "completeness",
        "Every record present in the source ledger is reproduced in the deliverable.",
        "CRITICAL",
    ),
    ("accuracy", "Each reproduced record's fields are accurate and untruncated.", "CRITICAL"),
    (
        "consistency",
        "The ledger's formatting is internally consistent across every record.",
        "MAJOR",
    ),
    ("editorial-quality", "The deliverable reads as a finished, publishable ledger.", "MAJOR"),
)


def build_report_editor_battery(
    *,
    min_records: int,
    required_fields: Sequence[str] | None = None,
    forbidden_markers: Sequence[str] | None = None,
    min_citation_ratio: float = 0.0,
    min_length: int = 1,
    quality_rubrics: Sequence[tuple[str, str, _Severity]] | None = None,
    evaluator_threshold: float = 0.7,
    name: str = "eurail-report-editor",
) -> OHMGateBattery:
    """Build the EURail 10-gate report-editor battery (``floor="and"``) — 6 deterministic structural
    gates + 4 evaluator quality gates. Every parameter (the ledger floor, the required fields, the
    forbidden markers, the editorial rubrics) is an INPUT the reviewer supplies for the real EURail;
    the defaults are a coherent JSON-ledger scaffold the unit tests exercise. ``floor="and"`` means
    every applicable gate must pass — the strict equivalence bar EURail's spec mandates."""
    fields = list(required_fields or [])
    forbidden = list(forbidden_markers or ["TODO", "DISPUTED", "needs-source", "PLACEHOLDER"])
    rubrics = list(quality_rubrics if quality_rubrics is not None else _DEFAULT_QUALITY_RUBRICS)

    deterministic = [
        OHMGateCheck(
            name="ledger-record-floor",
            kind="deterministic",
            check_ref="core/check/min-records",
            params={"min": min_records},
            severity="CRITICAL",
        ),
        OHMGateCheck(
            name="ledger-valid-json",
            kind="deterministic",
            check_ref="core/check/json-valid",
            severity="CRITICAL",
        ),
        OHMGateCheck(
            name="no-unresolved-markers",
            kind="deterministic",
            check_ref="core/check/no-forbidden",
            params={"terms": forbidden},
            severity="CRITICAL",
        ),
        OHMGateCheck(
            name="required-fields-present",
            kind="deterministic",
            check_ref="core/check/contains-all",
            params={"terms": fields},
            severity="MAJOR",
        ),
        OHMGateCheck(
            name="citation-coverage",
            kind="deterministic",
            check_ref="core/check/citation-coverage",
            params={"min_ratio": min_citation_ratio},
            severity="MAJOR",
        ),
        OHMGateCheck(
            name="min-length",
            kind="deterministic",
            check_ref="core/check/min-length",
            params={"min": min_length},
            severity="MINOR",
        ),
    ]
    evaluator = [
        OHMGateCheck(
            name=rname,
            kind="evaluator",
            rubric=rubric,
            params={"threshold": evaluator_threshold},
            severity=sev,
        )
        for (rname, rubric, sev) in rubrics
    ]
    return OHMGateBattery(
        name=name,
        description="EURail report-editor battery — ledger reproduction + editorial quality.",
        checks=deterministic + evaluator,
        floor="and",
    )


async def ledger_equivalence(
    deliverable: str,
    *,
    baseline_band: BaselineBand,
    battery: OHMGateBattery,
    evaluate: EvaluatorInvoke,
    context: dict[str, object] | None = None,
    predicates: dict[str, Predicate] | None = None,
) -> EquivalenceVerdict:
    """Score a compiled-team EURail deliverable. PASS iff the record count is WITHIN the imported
    team's band (band-relative equivalence) AND the report-editor battery passes (``floor="and"``).

    ``evaluate`` is the injected judge invoker for the battery's evaluator gates (the reviewer wires
    the real BYOM judge; tests inject a fake) — kept injected so this oracle needs no service."""
    record_count = count_ledger_records(deliverable)
    within = baseline_band.contains(record_count)
    battery_verdict = await run_battery(
        battery, deliverable, evaluate=evaluate, context=context, predicates=predicates
    )

    reasons: list[str] = []
    if not within:
        reasons.append(
            f"record count {record_count} is outside the imported-team band "
            f"[{baseline_band.lo}, {baseline_band.hi}]"
        )
    if not battery_verdict.passed:
        reasons.extend(f"gate {f.name!r}: {f.reason}" for f in battery_verdict.failures)

    return EquivalenceVerdict(
        passed=within and battery_verdict.passed,
        record_count=record_count,
        within_band=within,
        band_lo=baseline_band.lo,
        band_hi=baseline_band.hi,
        battery=battery_verdict,
        reasons=reasons,
    )
