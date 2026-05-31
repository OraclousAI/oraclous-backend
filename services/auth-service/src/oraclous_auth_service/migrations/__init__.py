"""Auth-service migrations (ORA-36 / R1-D1 onward).

Migrations that target the auth-service identity domain (``agents`` /
``agent_credentials``). Per ADR-001 / ADR-012 §1a, the auth-service is the
only home for credential and principal writes — substrate-hosted migrations
never raw-SQL these tables. Cross-domain migrations (those that also write
to Neo4j or the knowledge substrate) live here and compose substrate-side
helpers from ``oraclous_substrate.migrations`` for the substrate writes.
"""

from __future__ import annotations
