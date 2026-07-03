"""Compiler on-ramp helpers (domain layer — pure, no I/O). #635 (the re-scoped C-1).

The two deterministic pieces the compile/refine endpoints share:

- ``draft_catalog()`` — the surveyed capability catalog drafts are validated against: the #596
  seed inventory's tools (every seed tool is a real registered capability, per the seed contract).
  The live-registry union (``survey_catalog(inventory, registered)``) is a deliberate seam: when a
  registry survey lands on the request path, pass its slugs as ``registered`` here — one call
  site, no shape change.
- ``compose_objective()`` — folds the optional inputs/constraints/success-criteria fields of the
  Describe door (journey J1 s1) into the single prose objective ``build_compiler_team`` bakes into
  the planner's subgoal.
"""

from __future__ import annotations

import json
from typing import Any

from oraclous_ohm.seeds import default_seed_set, survey_catalog


def draft_catalog() -> list[str]:
    """The surveyed tool slugs a draft may draw from (seed inventory; live union is a seam)."""
    return survey_catalog(default_seed_set().inventory, [])


def compose_objective(
    objective: str,
    *,
    inputs: dict[str, Any] | None = None,
    constraints: str | None = None,
    success_criteria: str | None = None,
) -> str:
    """One prose objective for the compiler's planner — the Describe door's optional fields are
    appended as labelled sections so the planner sees them without any new manifest surface."""
    parts = [objective.strip()]
    if inputs:
        parts.append(f"Inputs (seed data the team receives at GO):\n{json.dumps(inputs)}")
    if constraints and constraints.strip():
        parts.append(f"Constraints:\n{constraints.strip()}")
    if success_criteria and success_criteria.strip():
        parts.append(f"Success criteria:\n{success_criteria.strip()}")
    return "\n\n".join(parts)
