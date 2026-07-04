"""Compiler on-ramp helpers (domain layer — pure, no I/O). #635 (the re-scoped C-1).

The two deterministic pieces the compile/refine endpoints share:

- ``draft_catalog(registered)`` — the surveyed capability catalog drafts are validated against:
  the #596 seed inventory's tools UNION the org's LIVE registry capabilities (#638). Every seed
  tool is a real registered capability (the seed contract); ``registered`` (the org's live
  registry NAMES, fetched by the caller — one seam, all three consumers: compiler survey,
  assemble/validation, refine/refine-nl) makes a deployed connector (``GitHub Sink`` → slug
  ``github-sink``) admissible too. ``survey_catalog`` de-dups + normalises via ``_slug``; the
  capability-absence gate stays fail-closed for a genuinely unregistered tool.
- ``compose_objective()`` — folds the optional inputs/constraints/success-criteria fields of the
  Describe door (journey J1 s1) into the single prose objective ``build_compiler_team`` bakes into
  the planner's subgoal.
"""

from __future__ import annotations

import json
from typing import Any

from oraclous_ohm.seeds import default_seed_set, survey_catalog


def draft_catalog(registered: list[str] | None = None) -> list[str]:
    """The surveyed tool slugs a draft may draw from: the #596 seed inventory UNIONed with the
    org's LIVE registry capability names (#638). ``registered`` is the org's live capability names
    (from ``RegistryClient.list_capabilities`` — the caller degrades to ``None``/``[]`` seed-only
    on a registry outage, never fail-open). ``survey_catalog`` normalises + de-dups the union."""
    return survey_catalog(default_seed_set().inventory, registered or [])


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
