"""Service configuration (ORAA-4 §21 core layer) — env → Settings.

Pydantic-settings; each knob is a `KGS_`-prefixed env var with a dev-friendly default so the
service runs from `docker compose` with no secrets (the dev-auth + hashing-embedder seams). S2 adds
the ingestion surface: Neo4j (write role kgs_writer), Redis/Celery, and the embedder/extractor
seams. `neo4j_uri` has NO hardcoded default (ORAA-53) — it must come from `KGS_NEO4J_URI`; when
unset the service runs in graph-CRUD-only mode and ingestion endpoints report the missing substrate.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KGS_", extra="ignore")

    # --- identity seam. `gateway` (production, ADR-018): trust the gateway's verified
    # X-Principal-*/X-Organisation-Id headers, gated by X-Internal-Key — no token validation here.
    # `dev`: a fixed bearer for the standalone smoke. `jwt`: decode a real HS256 token directly
    # (used when a caller reaches the service without the gateway). ---
    auth_mode: Literal["gateway", "dev", "jwt"] = "dev"
    dev_bearer: str = "dev-token"
    dev_user_id: str = "00000000-0000-0000-0000-0000000000d5"
    dev_org_id: str = "00000000-0000-0000-0000-00000000050a"
    # gateway mode: the shared secret the gateway sends as X-Internal-Key; fail-closed if unset.
    internal_service_key: str | None = None
    # jwt mode: the shared HS256 secret the auth-service signs with (compose injects KGS_JWT_SECRET
    # = the auth-service JWT_SECRET). No default — jwt mode fails closed without it.
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"

    # --- Postgres (graph metadata + ingestion jobs) ---
    database_url: str = "postgresql+asyncpg://oraclous:oraclous@postgres:5432/oraclous"

    # --- Neo4j (kgs_writer role, ORAA-53). No hardcoded URI default. ---
    neo4j_uri: str | None = None
    neo4j_user: str = "kgs_writer"
    neo4j_password: str = "kgs-writer-pass"  # noqa: S105 — dev default; prod injects via secret
    neo4j_database: str | None = None

    # --- Redis / Celery (async ingestion spine) ---
    redis_url: str = "redis://redis:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    # --- ingestion seams (key-free defaults: deterministic hashing embedder, no LLM extraction) ---
    embedder: Literal["hashing", "openai"] = "hashing"
    embedding_dim: int = 512
    extractor: Literal["null", "openai"] = "null"
    # Shared OpenAI-compatible API key (embedder + extractor). The platform's single LLM key is
    # OpenRouter's; compose injects KGS_OPENAI_API_KEY=${OPENROUTER_API_KEY}.
    openai_api_key: str | None = None
    # OpenAI-compatible base URL the embedder + extractor clients point at. Default = OpenRouter, so
    # the one platform key reaches Claude/OpenAI/etc. behind one endpoint. The stock OpenAI embedder
    # model `text-embedding-3-small` is only served by api.openai.com, so an OpenAI embedder caller
    # must override this to https://api.openai.com/v1 (or set KGS_OPENAI_BASE_URL accordingly).
    openai_base_url: str = "https://openrouter.ai/api/v1"
    # Chat model used for LLM entity/relation extraction (only read when extractor == "openai").
    # An OpenRouter-style `<provider>/<model>` id; a strong instruction-follower is preferred.
    extractor_model: str = "openai/gpt-4o-mini"
    # Max concurrent LLM calls across chunks in one document extraction (the library fans out).
    # Env-tunable via KGS_EXTRACTOR_MAX_CONCURRENCY (OpenRouter handles the concurrency); raise it
    # to speed up free-text entity extraction on multi-chunk documents.
    extractor_max_concurrency: int = 10

    # --- community detection (#303) ---
    # At or below this entity count a detect runs INLINE (the request blocks for an immediate
    # result); above it, it enqueues a Celery job. A few hundred is a true tiny-graph floor — a
    # single Louvain pass on that many nodes is sub-second.
    community_sync_entity_threshold: int = 300
    # Hard ceiling: a graph with MORE entities than this SKIPS detection with a clear reason rather
    # than risk heap exhaustion projecting a huge graph on the 512m Neo4j (legacy
    # COMMUNITY_DETECTION_MAX_ENTITIES). 0 disables the ceiling.
    community_max_entities: int = 500_000
    # Max communities summarised in one inline summarize call (cost guard). Above it the call
    # returns 0 (the caller routes large summarise to the async path). 0 disables the cap.
    community_summarize_max_inline: int = 200

    # --- SQL relational ingest (#307) ---
    # The credential broker the SQL ingest resolves a stored `connection_string` from by id.
    # `fake` (dev/CI default): a deterministic, key-free broker that returns
    # `credential_broker_fake_dsn` for any id — so the SQL-ingest path reaches a real end-to-end
    # test without the broker. `real`: POST /internal/resolve-credential with X-Internal-Key.
    credential_broker_mode: Literal["fake", "real"] = "fake"
    credential_broker_base_url: str | None = None
    # The DSN the FAKE broker returns (only read in fake mode). Defaults to this service's own
    # Postgres so a dev SQL ingest has a live DB to read; override per test/deployment.
    credential_broker_fake_dsn: str = "postgresql://oraclous:oraclous@postgres:5432/oraclous"
    # TCP egress guard (#307, Option B). When True (single-tenant / dev — mirrors the HRS egress
    # `allow_private`), a SQL ingest may target a private/loopback/internal DB host (so a user can
    # ingest from a local or internal DB); the link-local / cloud-metadata range stays blocked in
    # EITHER mode. When False (multi-tenant), private/loopback/internal hosts are blocked. Dev
    # default True so the docker-compose Postgres (a private `postgres` host) is reachable.
    sql_ingest_allow_private_egress: bool = True
    # Hard ceiling on rows fetched per table in a full_snapshot SQL ingest (cost / heap guard).
    sql_ingest_max_rows_per_table: int = 50_000

    # --- similarity auto-trigger (#310, legacy SIMILARITY_AUTO_TRIGGER_ON_INGEST) ---
    # When True, a structured ingest with NO authored `similarities[]` rule still runs the content-
    # similarity pass: one default SIMILAR_TO rule is synthesised per node rule over the node's best
    # text field, so records connect by content without the author writing a rule. OFF by default —
    # opt-in via KGS_SIMILARITY_AUTO_TRIGGER (an explicit `similarities[]` block always wins and is
    # never overridden). The synthesised rule uses the floor below; per-type tuning needs an
    # authored rule.
    similarity_auto_trigger: bool = False
    # The min cosine the auto-synthesised default similarity rule applies (only read when
    # similarity_auto_trigger is on). A conservative floor; author a rule for finer control.
    similarity_auto_min_score: float = 0.85

    @property
    def sync_database_url(self) -> str:
        """psycopg3 (sync) DSN derived from the async one — used by Alembic + seed_dev."""
        return self.database_url.replace("+asyncpg", "+psycopg")

    @property
    def celery_broker(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def celery_backend(self) -> str:
        return self.celery_result_backend or self.redis_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
