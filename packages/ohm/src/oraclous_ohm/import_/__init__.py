"""Adoption-First import (ADR-034): read an existing agent setup and emit a runnable OHM v1.1 Team
Harness. Pure (filesystem-in, OHM-out). Slice 1 (#405) is the ``.claude/agents/*.md`` parser; the
frontmatter->member mapping, skill inlining, charter/orchestrator adapters, DAG-from-source, and the
dry-run land in #406-#409.

The validator seam (#593, ADR-047 "one validator, two on-ramps") is exported here so BOTH on-ramps
reach it identically: ``import_setup`` (filesystem-in) and ``assemble_and_report`` (source-agnostic,
members-in — the E10 prose compiler's reviewer member). No filesystem dependency on the latter.
"""

from oraclous_ohm.import_._flags import ImportFlag
from oraclous_ohm.import_.setup import (
    ImportReport,
    ImportResult,
    assemble_and_report,
    import_setup,
    render_report,
)

__all__ = [
    "ImportFlag",
    "ImportReport",
    "ImportResult",
    "assemble_and_report",
    "import_setup",
    "render_report",
]
