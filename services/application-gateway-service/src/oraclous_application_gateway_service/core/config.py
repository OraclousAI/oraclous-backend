"""application-gateway settings (ORAA-4 §21 core layer).

The gateway is a reverse-proxy edge that, since R6 Slice 3 (ADR-019), also owns a small Postgres for
the integration-key store. Settings carry the upstream base URLs
(the route table is built from these), the identity seam (mirroring the substrate services:
``dev`` binds a fixed principal/org from a fixed bearer, ``jwt`` verifies the real HS256 token with
the shared ``AUTH_JWT_SECRET``), and the CORS allow-list. Constructed lazily via ``get_settings``.
"""

from __future__ import annotations

from functools import lru_cache

from oraclous_governance import MissingSecretError, is_prod, require_secret
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Dev-only fallbacks (used IFF RUN_MODE != prod). In prod a missing/empty value raises at
# get_settings() construction (fail closed, T6 / ADR-008). See oraclous_governance.require_secret.
_DEV_INTERNAL_SERVICE_KEY = "dev-internal-key"  # noqa: S105 — dev default, gated by RUN_MODE
_DEV_JWT_SECRET = "dev-jwt-secret"  # noqa: S105 — dev default, gated by RUN_MODE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "oraclous-application-gateway-service"
    VERSION: str = "0.0.1"

    # --- published public contract (ADR-015) ---
    # Path to openapi/v1.yaml; empty => the loader searches upward for it (in-image + source).
    OPENAPI_SPEC_PATH: str = ""

    # --- upstream base URLs (the route table maps path-prefixes onto these) ---
    AUTH_SERVICE_URL: str = "http://auth-service:8000"
    CREDENTIAL_BROKER_URL: str = "http://credential-broker-service:8000"
    KNOWLEDGE_GRAPH_URL: str = "http://knowledge-graph-service:8000"
    KNOWLEDGE_RETRIEVER_URL: str = "http://knowledge-retriever-service:8000"
    CAPABILITY_REGISTRY_URL: str = "http://capability-registry-service:8000"
    HARNESS_RUNTIME_URL: str = "http://harness-runtime-service:8000"
    EXECUTION_ENGINE_URL: str = "http://execution-engine-service:8000"

    # --- proxy behaviour ---
    UPSTREAM_CONNECT_TIMEOUT: float = 5.0
    UPSTREAM_READ_TIMEOUT: float = 30.0

    # --- edge protection (R6 Slice 2): Redis-backed rate limit + request-size guard ---
    # DB 2 isolates the edge-limiter keyspace (DB 0 = auth, DB 1 = execution-engine).
    REDIS_URL: str = "redis://redis:6379/2"
    # Short socket timeouts so a Redis PARTITION fails the limiter OPEN almost instantly instead of
    # blocking the sole ingress on the OS connect timeout (the limiter is on every request).
    REDIS_SOCKET_TIMEOUT_SECONDS: float = 0.5
    # edge-wide per-client-IP fixed window (ops-tunable; not the auth limiter's hard 10/60).
    EDGE_RATE_LIMIT: int = 600
    EDGE_RATE_WINDOW_SECONDS: int = 60
    # per-subscription webhook-ingress limit (R7-SEC S3) — above the per-IP edge floor; one abused
    # subscription is throttled independently. Fail-open, like the edge limiter.
    WEBHOOK_RATE_LIMIT: int = 600
    WEBHOOK_RATE_WINDOW_SECONDS: int = 60
    # rate-limiter behaviour on a Redis OUTAGE (ADR-021 §1). Default TRUE = fail-OPEN: a transient
    # Redis blip must not self-DoS the sole external ingress (availability of the edge outweighs
    # strict limiting during a blip). Every fail-open still emits a structured alert (never silent).
    # Set FALSE for a hardened, Redis-HA-backed deploy: an outage then 503s (fail-CLOSED) rather
    # than silently dropping the limit. The default MUST stay the safe (open) value — a wrong False
    # + no HA story 503s all traffic.
    RATE_LIMIT_ALLOW_DURING_OUTAGE: bool = True
    # request-body cap (fail-closed); conservative default, per-route override is a later slice.
    MAX_REQUEST_BODY_BYTES: int = 10 * 1024 * 1024
    # X-Forwarded-For trust boundary: 0 = ignore XFF, key on the socket peer (no LB).
    # Raise ONLY in lockstep with adding that many of our own proxies; each extra hop is spoofable.
    TRUSTED_PROXY_COUNT: int = 0

    # --- identity seam (mirrors substrate: dev binds a fixed principal; jwt verifies HS256) ---
    GATEWAY_AUTH_MODE: str = "dev"  # "dev" | "jwt"
    DEV_BEARER: str = "dev-token"  # the dev admin (org_role=admin) — existing dev management flows
    DEV_USER_ID: str = "00000000-0000-0000-0000-0000000000e6"
    DEV_ORG_ID: str = "00000000-0000-0000-0000-00000000050a"
    # a second dev bearer for a plain MEMBER in the same org (R7-SEC S2) — lets the roles floor be
    # exercised live (member -> 403 on the admin-gated management ops).
    DEV_MEMBER_BEARER: str = "dev-member-token"
    DEV_MEMBER_USER_ID: str = "00000000-0000-0000-0000-0000000000e7"
    # Empty sentinel default; resolved fail-closed in _resolve_failclosed_secrets (require_secret):
    # in prod a missing/empty JWT_SECRET raises, in dev it falls back to the dev default. JWT_SECRET
    # is only consumed when GATEWAY_AUTH_MODE="jwt".
    JWT_SECRET: str | None = None
    JWT_ALGORITHM: str = "HS256"

    # --- edge-auth attestation: the shared secret injected as X-Internal-Key on every forwarded
    # request so upstreams can prove a request actually came through the gateway (ADR-018). Empty
    # sentinel default; resolved fail-closed below (no publicly-known default reaches prod). ---
    INTERNAL_SERVICE_KEY: str = ""

    # --- CORS (terminated once at the edge); comma-separated origins. "*" is the dev default but is
    # ILLEGAL in prod (a wildcard at the sole external edge is fail-open) — see the validator. ---
    GATEWAY_CORS_ORIGINS: str = "*"

    # --- gateway-owned datastore (R6 Slice 3, ADR-019): the integration-key + published-agent +
    # chat + webhook-subscription store. The ORG-BOUND engine DSN (ADR-030 §3): the request CRUD
    # path connects here as the NOSUPERUSER ``oraclous_app`` role so the FORCE'd RLS policy bites.
    # The two pre-auth PRODUCER reads (get_by_prefix / get_by_id), Alembic, and the rls-role
    # bootstrap use ``owner_database_url`` (the owner) instead — see below. ---
    DATABASE_URL: str = "postgresql+asyncpg://oraclous:oraclous@postgres:5432/oraclous"

    # --- the OWNER engine DSN (ADR-030 §3 carve-out, mirrors auth's owner-engine split). The two
    # pre-auth producer lookups — integration-key ``get_by_prefix`` and webhook-subscription
    # ``get_by_id`` — resolve an org/credential BEFORE any org context, so they MUST run on a role
    # that bypasses RLS (the owner/superuser); else FORCE'd RLS fails them closed to zero rows and
    # breaks integration-key auth + inbound webhooks (the HARD RULE). Defaults to the OWNER DSN; in
    # the deployed stack the org-bound ``DATABASE_URL`` flips to oraclous_app while this stays the
    # owner. Alembic + the bootstrap derive their owner DSN from this too (``sync_database_url``).
    OWNER_DATABASE_URL: str | None = None

    # --- Postgres RLS backstop (ADR-030 / #353) ---
    # When true, the service asserts at startup (web lifespan) that the ORG-BOUND runtime DB role is
    # NOSUPERUSER/NOBYPASSRLS (a bypassing role silently voids the FORCE'd RLS policy — T1-M3) and
    # FAILS CLOSED otherwise. The deployed api connects as oraclous_app with this on; migrations,
    # the rls-role bootstrap, and the owner-engine producer reads keep running as the owner and
    # never set it. Default false so a test/local run that intentionally uses the owner DSN need not
    # provision the app role. (The gateway runs no Celery worker that touches these tables, so there
    # is no worker_process_init mirror — the web lifespan is the sole assertion chokepoint.)
    GATEWAY_RLS_ASSERT_RUNTIME_ROLE: bool = False

    @model_validator(mode="after")
    def _resolve_failclosed_secrets(self) -> Settings:
        """Apply fail-closed secret resolution + the prod CORS-wildcard ban (T6 / ADR-008).

        Runs at construction so a misconfigured prod deploy fails fast (like credential-broker's
        no-default settings) instead of silently serving a publicly-known key. Dev/local-docker
        (RUN_MODE unset or dev) keep booting with the dev defaults — behaviour identical to before.
        """
        self.INTERNAL_SERVICE_KEY = require_secret(
            "INTERNAL_SERVICE_KEY", dev_default=_DEV_INTERNAL_SERVICE_KEY
        )
        # JWT_SECRET: in prod a missing/empty value fails closed here; in dev it stays None (the
        # GATEWAY_AUTH_MODE=jwt path in core/auth.py already raises on a None secret, so dev
        # behaviour is unchanged — the dev default exists only so the field is a non-empty literal
        # under require_secret's prod check, never substituted into dev).
        if not self.JWT_SECRET:
            require_secret("JWT_SECRET", dev_default=_DEV_JWT_SECRET)
        # A "*" wildcard CORS allow-list at the sole external edge is fail-open; banned in prod.
        if is_prod() and "*" in self.cors_origins:
            raise MissingSecretError(
                "GATEWAY_CORS_ORIGINS must be an explicit origin allow-list when RUN_MODE=prod; "
                'the wildcard "*" is not permitted at the external edge.'
            )
        return self

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.GATEWAY_CORS_ORIGINS.split(",") if o.strip()]

    @property
    def owner_database_url(self) -> str:
        """The OWNER (superuser / BYPASSRLS) async DSN the two pre-auth producer reads resolve on
        (ADR-030 §3).

        Defaults to ``DATABASE_URL`` so a single-DSN deploy/test (no RLS split) behaves exactly as
        before — both engines are the owner and RLS is a no-op. In the deployed RLS stack
        ``DATABASE_URL`` flips to the org-bound oraclous_app role while ``OWNER_DATABASE_URL`` stays
        the owner, so only the producer reads bypass RLS. Alembic + the bootstrap derive their owner
        DSN from this too (``sync_database_url``)."""
        return self.OWNER_DATABASE_URL or self.DATABASE_URL

    @property
    def sync_database_url(self) -> str:
        """The synchronous psycopg DSN Alembic + the rls-role bootstrap use (swaps the asyncpg
        driver for psycopg). Always the OWNER DSN — migrations + bootstrap are owner privileges."""
        return self.owner_database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()
