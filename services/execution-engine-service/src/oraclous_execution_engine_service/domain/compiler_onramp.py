"""Compiler on-ramp helpers (domain layer — pure, no I/O). #635 (the re-scoped C-1).

``draft_catalog()`` — the surveyed capability catalog drafts are validated against: the #596
seed inventory's tools (every seed tool is a real registered capability, per the seed contract).
The live-registry union (``survey_catalog(inventory, registered)``) is a deliberate seam: when a
registry survey lands on the request path, pass its slugs as ``registered`` here — one call
site, no shape change.
"""

from __future__ import annotations

from oraclous_ohm.seeds import default_seed_set, survey_catalog


def draft_catalog() -> list[str]:
    """The surveyed tool slugs a draft may draw from (seed inventory; live union is a seam)."""
    return survey_catalog(default_seed_set().inventory, [])
