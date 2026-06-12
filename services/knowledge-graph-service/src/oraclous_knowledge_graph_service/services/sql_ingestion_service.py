"""SQL relational ingestion use-case (ORAA-4 §21 services layer).

Orchestrates a SQL source into the graph, reusing the EXISTING deterministic recipe engine + the
org-scoped writer (no new graph-write path):

    resolve credential (broker, by id)  →  parse + egress-validate the DSN  →  connect (pinned IP)
    →  introspect schema (information_schema)  →  fetch rows (full_snapshot)  →  decompose to a
    relational StructuralRepresentation  →  pick a recipe (supplied/stored else default-relational)
    →  run the recipe engine over the org-scoped RecipeGraphWriter.

The connector half is async (asyncpg / aiomysql); the engine half is synchronous (the engine + the
sync Neo4j driver), so the engine call is offloaded to a thread. Org id is resolved by the caller
and passed in explicitly (the org is server-injected — the caller cannot override it). The graph
scope is the path ``graph_id``. Failure modes (bad DSN, blocked host, broker miss, no tables) raise
``SqlIngestionError`` for a clean upstream HTTP map.
"""

from __future__ import annotations

import asyncio

from oraclous_knowledge_graph_service.core.config import Settings, get_settings
from oraclous_knowledge_graph_service.domain.connectors.sql_connector import (
    DbSyncMode,
    SqlConnector,
    SqlConnectorError,
    parse_and_validate_dsn,
)
from oraclous_knowledge_graph_service.domain.ontology import Ontology
from oraclous_knowledge_graph_service.domain.structural import ExtractionMode
from oraclous_knowledge_graph_service.domain.tcp_egress import EgressBlockedError
from oraclous_knowledge_graph_service.repositories.recipe_write_repository import RecipeGraphWriter
from oraclous_knowledge_graph_service.services.credential_client import (
    CredentialBrokerPort,
    CredentialResolutionError,
)
from oraclous_knowledge_graph_service.services.recipes.engine import get_recipe_engine
from oraclous_knowledge_graph_service.services.structured.relational import (
    build_default_relational_recipe,
    decompose_relational,
)


class SqlIngestionError(Exception):
    """A SQL relational ingest failed (credential, egress, connect, introspect, or empty schema)."""


class SqlIngestionService:
    def __init__(
        self,
        *,
        driver,
        broker: CredentialBrokerPort,
        organisation_id: str,
        database: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._driver = driver
        self._broker = broker
        self._org = organisation_id
        self._db = database
        self._settings = settings or get_settings()
        self._engine = get_recipe_engine()

    async def ingest(
        self,
        *,
        graph_id: str,
        credential_id: str,
        sync_mode: DbSyncMode = DbSyncMode.FULL_SNAPSHOT,
        schema: str | None = None,
        recipe: dict | None = None,
        ontology: Ontology | None = None,
    ) -> dict:
        # 1) Resolve the connection_string from the broker (server-injected org; caller's cred id).
        # KNOWN LIMITATION (tracked, not introduced by #307): the resolve is ORG-scoped, not
        # USER-scoped — a same-org member can ingest using a co-member's `credential_id`. This is
        # the PRE-EXISTING platform pattern (matches the capability-registry tool-execution
        # contract) and is within-org (never cross-org), so it is in scope of the broader authz
        # hardening (R7-SEC/R8), not this connector. The broker contract is unchanged here.
        try:
            dsn = await self._broker.resolve_connection_string(
                organisation_id=self._org, credential_id=credential_id
            )
        except CredentialResolutionError as exc:
            raise SqlIngestionError(f"credential resolution failed: {exc}") from exc

        # 2) Parse the DSN + run the TCP egress guard (Option B) BEFORE any connect; pin the IP.
        try:
            params = parse_and_validate_dsn(
                dsn, allow_private=self._settings.sql_ingest_allow_private_egress
            )
        except EgressBlockedError as exc:
            raise SqlIngestionError(f"DB host blocked by egress guard: {exc}") from exc
        except SqlConnectorError as exc:
            raise SqlIngestionError(str(exc)) from exc

        # 3) Connect (to the pinned IP) → introspect → fetch rows (full_snapshot only).
        connector = SqlConnector(params, schema=schema)
        try:
            await connector.connect()
            snapshot = await connector.introspect_schema()
            if not snapshot.tables:
                raise SqlIngestionError(
                    f"no tables found in schema {snapshot.schema_name!r} of {snapshot.database!r}"
                )
            rows_by_table: dict[str, list[dict]] = {}
            if sync_mode == DbSyncMode.FULL_SNAPSHOT:
                cap = self._settings.sql_ingest_max_rows_per_table
                for table in snapshot.tables:
                    rows_by_table[table.name] = await connector.fetch_rows(
                        table.name, snapshot, limit=cap
                    )
        except SqlConnectorError as exc:
            raise SqlIngestionError(str(exc)) from exc
        finally:
            await connector.close()

        # 4) Decompose to the relational StructuralRepresentation + pick the recipe.
        mode = (
            ExtractionMode.FULL if sync_mode == DbSyncMode.FULL_SNAPSHOT else ExtractionMode.SAMPLE
        )
        representation = decompose_relational(snapshot, rows_by_table, mode)
        active_recipe = recipe or build_default_relational_recipe(snapshot)

        # 5) Run the deterministic engine over the org-scoped writer (off the event loop — the
        # engine uses the SYNCHRONOUS Neo4j driver, same as the CSV/JSON structured path).
        writer = RecipeGraphWriter(
            self._driver, graph_id=graph_id, organisation_id=self._org, database=self._db
        )
        result = await asyncio.to_thread(
            self._engine.execute, active_recipe, representation, writer, ontology=ontology
        )
        data = result.as_dict()
        data["dialect"] = snapshot.dialect.value
        data["database"] = snapshot.database
        data["schema"] = snapshot.schema_name
        data["tables_introspected"] = len(snapshot.tables)
        data["sync_mode"] = sync_mode.value
        return data
