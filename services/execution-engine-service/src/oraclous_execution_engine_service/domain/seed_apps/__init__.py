"""The Oraclous-provided apps, and how their documents are built (domain layer, #932).

These ship with the engine rather than being registered by hand. The Validation Desk previously
existed only as a team draft in whichever throwaway organisation a script had authenticated as, with
its id hand-copied into a frontend environment variable — so it belonged to one tenant and was
invisible to everyone else. Seeding it into the platform organisation at startup, and letting the
widened read carry it, is what makes it a real default.

``manifest.json`` under each app's directory is the committed source of truth: a hand-authored OHM
v1.1 Team Harness. It lives inside the service package because the seed runs in the engine's own
image, where ``scripts/`` does not exist.
"""

from __future__ import annotations

from oraclous_execution_engine_service.domain.seed_apps.build import (
    SeedApp,
    build_seed_apps,
)

__all__ = ["SeedApp", "build_seed_apps"]
