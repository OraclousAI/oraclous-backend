"""#593 — ONE validator, TWO on-ramps (ADR-047). The source-agnostic ``assemble_and_report`` takes
already-built ``members`` (the E10 prose compiler's drafter shape, NO filesystem) and produces the
SAME ``ImportReport`` the filesystem importer does — and ``import_setup`` now CALLS it, so there is
no second validator to drift. A blocking ``extra_flag`` (a hallucinated tool the surveyor caught)
drives ``would_block`` on the prose path exactly as on the import path.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest
from oraclous_ohm.import_ import (  # the EXPORTED seam — importable with no filesystem dependency
    ImportFlag,
    ImportResult,
    assemble_and_report,
    import_setup,
)
from oraclous_ohm.manifest import OHMMember

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(role: str, depends_on: list[str] | None = None) -> OHMMember:
    return OHMMember(
        role=role, kind="agent", manifest_ref=f"org:x/{role}@1", depends_on=depends_on or []
    )


def test_a_compiled_shape_input_assembles_and_reports() -> None:
    # the compiler hands a hand-built members[] (NOT a dir) — the same validator runs.
    members = [_m("planner"), _m("writer", ["planner"])]
    result = assemble_and_report(
        "compiled-team", members, owner_organization_id=_ORG, shape="compiled"
    )
    assert isinstance(result, ImportResult)
    assert result.report.shape == "compiled"  # the report records WHICH on-ramp produced the team
    assert result.report.member_count == 2
    assert result.report.stages == [["planner"], ["writer"]]
    assert result.report.would_block is False  # capabilities resolve → ready
    assert result.manifest is not None and result.manifest.is_team()


def test_a_blocking_extra_flag_blocks_the_prose_path() -> None:
    # the surveyor caught a hallucinated tool → an F-CAPABILITY-MISSING blocking flag — the gate
    # that rejects a hallucinated tool on the import path drives would_block on the prose path.
    members = [_m("planner"), _m("writer", ["planner"])]
    bad = ImportFlag(
        code="F-CAPABILITY-MISSING",
        severity="blocking",
        member_role="writer",
        message="writer references an unsurveyed tool 'teleport'",
    )
    result = assemble_and_report(
        "compiled-team", members, owner_organization_id=_ORG, shape="compiled", extra_flags=[bad]
    )
    assert result.report.would_block is True  # a blocking flag halts GO — identical to the importer
    assert any("F-CAPABILITY-MISSING" in b for b in result.report.blocking)


def test_empty_members_is_a_blocking_report_not_a_crash() -> None:
    # #593 fail-closed: a drafter that produced NO members (or an empty dir) gets a blocking report,
    # never a pydantic crash — the new public seam honours fail-closed when it is introduced.
    result = assemble_and_report("empty", [], owner_organization_id=_ORG, shape="compiled")
    assert result.manifest is None
    assert result.report.would_block is True
    assert any("F-NO-MEMBERS" in b for b in result.report.blocking)


def test_one_validator_two_on_ramps_agree_structurally() -> None:
    # the SAME logical team validated via the filesystem importer AND via the source-agnostic entry
    # produces structurally-equal reports — one validator, two on-ramps (asserted in code).
    root = Path(tempfile.mkdtemp())
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    (adir / "planner.md").write_text(
        "---\nname: planner\n---\nPlan.\n\n## Handoff\n**Next agent**: writer\n"
    )
    (adir / "writer.md").write_text("---\nname: writer\n---\nWrite.\n")
    imported = import_setup(root, owner_organization_id=_ORG, name="compiled-team")

    # the compiler lowers the same two members by hand (planner -> writer)
    members = [_m("planner"), _m("writer", ["planner"])]
    compiled = assemble_and_report(
        "compiled-team", members, owner_organization_id=_ORG, shape="compiled"
    )

    assert compiled.report.member_count == imported.report.member_count
    assert compiled.report.stages == imported.report.stages
    assert compiled.report.would_block == imported.report.would_block
    # the on-ramp is the ONLY recorded difference
    assert imported.report.shape == "agent-team" and compiled.report.shape == "compiled"
