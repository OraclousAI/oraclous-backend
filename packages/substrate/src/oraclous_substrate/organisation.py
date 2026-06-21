"""Seed organisation for single-org deployments (A1, AC#5).

A1 makes ``organisation_id`` mandatory on every substrate primitive. A
single-organisation deployment keeps behaving as before by scoping everything
under this stable, well-known seed organisation — so existing single-tenant
rows stay readable and writers always have an org to scope to (ADR-006).
"""

from __future__ import annotations

import uuid

SEED_ORGANISATION_ID: uuid.UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")
