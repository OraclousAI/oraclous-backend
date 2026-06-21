"""Substrate storage schema: organisation-scoped Postgres + Neo4j (A1).

``organisation_id`` is the outermost tenancy scope on every substrate storage
declaration. Each store's ``apply()`` is idempotent so a deployment can re-run
it safely.
"""

from __future__ import annotations
