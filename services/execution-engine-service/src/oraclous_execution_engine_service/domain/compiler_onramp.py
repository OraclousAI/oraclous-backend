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
from typing import Any, Literal

from oraclous_ohm.seeds import catalog_slug, default_seed_set, survey_catalog

Substrate = Literal["graph", "file"]

#: The tools that write to / read from the per-org FILE sandbox. Hidden from the drafter's menu
#: under the graph substrate (#694): filtering beats remapping at this layer, because the drafter
#: cannot pick what it is not shown and no rewrite step is needed afterwards. The remap in
#: ``import_.mapping`` stays as the backstop for a file tool that arrives by another route (an
#: import, a hand-edited draft, a team compiled before this fix).
#:
#: ``bash`` is deliberately NOT here. It is the rare sandbox exec fallback (#507), not a
#: deliverable sink, and it is not what #694 reports.
_FILE_SUBSTRATE_TOOLS = frozenset({"read", "write", "edit", "grep", "glob"})


def draft_catalog(
    registered: list[str] | None = None, *, substrate: Substrate = "graph"
) -> list[str]:
    """The surveyed tool slugs a draft may draw from: the #596 seed inventory UNIONed with the
    org's LIVE registry capability names (#638). ``registered`` is the org's live capability names
    (from ``RegistryClient.list_capabilities`` — the caller degrades to ``None``/``[]`` seed-only
    on a registry outage, never fail-open). ``survey_catalog`` normalises + de-dups the union.

    Under the default graph ``substrate`` (ADR-040 Decision 7, cloud-first) the FILE tools are
    withheld — including one arriving from the org's live registry under a colliding name, which
    would otherwise smuggle the same failure back in. The default is what actually ships: every
    existing caller passes no substrate, so a default of ``file`` would reintroduce #694 silently.
    """
    slugs = survey_catalog(default_seed_set().inventory, registered or [])
    if substrate == "file":
        return slugs
    return [s for s in slugs if s not in _FILE_SUBSTRATE_TOOLS]


def draft_catalog_described(
    registered: list[dict[str, str]] | None = None, *, substrate: Substrate = "graph"
) -> list[dict[str, str]]:
    """The same catalog as ``draft_catalog``, each entry paired with what the tool DOES (#713).

    ``registered`` is the org's live rows (``RegistryClient.list_capability_rows`` — name +
    description). Entries come back in ``draft_catalog`` order, one per slug, carrying
    ``description`` ONLY when the registry supplied a non-empty one. A seed-inventory tool has no
    descriptor row and therefore no description: it renders as its bare name. Nothing is invented
    for it — a made-up blurb is worse than none, because the drafter would believe it.

    #694: the menu a MODEL reads and the list a GATE diffs against share one filter, because that
    divergence is exactly how a file tool survived a validator."""
    rows = registered or []
    described = {
        slug: row["description"]
        for row in rows
        if (slug := catalog_slug(row.get("name", ""))) and row.get("description")
    }
    return [
        {"name": slug, **({"description": described[slug]} if slug in described else {})}
        for slug in draft_catalog([row.get("name", "") for row in rows], substrate=substrate)
    ]


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
