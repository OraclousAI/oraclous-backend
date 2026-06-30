"""The Harness Compiler (E10 / ADR-047): a prose objective → a schema-valid OHM v1.1 Team Harness,
built AS a Team Harness (planner → capability-surveyor → manifest-drafter → reviewer). The
greenfield surface is the prose front door + the four member sub-harness bodies + the prose→team
lowering — everything below (the assembler, the dry-run validator, run_team) is shipped and reused.

``validate_draft`` is the reviewer's capability-absence GATE: it diffs the drafted tools[] against
the surveyed catalog and runs the SAME ``assemble_and_report`` dry-run the importer uses — one
validator, two on-ramps. It returns a CODED ``would_block`` verdict (#594), never a model opinion.
"""

from oraclous_ohm.compiler.refine import (
    AddDependsOn,
    AddMember,
    ChangeKind,
    RefineOp,
    RefineResult,
    SetFanOut,
    apply_refine,
    parse_op,
)
from oraclous_ohm.compiler.team import build_compiler_team
from oraclous_ohm.compiler.validate import validate_draft

__all__ = [
    "AddDependsOn",
    "AddMember",
    "ChangeKind",
    "RefineOp",
    "RefineResult",
    "SetFanOut",
    "apply_refine",
    "build_compiler_team",
    "parse_op",
    "validate_draft",
]
