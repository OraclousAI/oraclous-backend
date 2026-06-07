"""application-gateway settings (ORAA-4 §21 core layer).

The gateway is a stateless reverse-proxy edge — no database. Settings carry the upstream base URLs
(the route table is built from these), the identity seam (mirroring the substrate services:
``dev`` binds a fixed principal/org from a fixed bearer, ``jwt`` verifies the real HS256 token with
the shared ``AUTH_JWT_SECRET``), and the CORS allow-list. Constructed lazily via ``get_settings``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- identity seam (mirrors substrate: dev binds a fixed principal; jwt verifies HS256) ---
    GATEWAY_AUTH_MODE: str = "dev"  # "dev" | "jwt"
    DEV_BEARER: str = "dev-token"
    DEV_USER_ID: str = "00000000-0000-0000-0000-0000000000e6"
    DEV_ORG_ID: str = "00000000-0000-0000-0000-00000000050a"
    JWT_SECRET: str | None = None
    JWT_ALGORITHM: str = "HS256"

    # --- edge-auth attestation: the shared secret injected as X-Internal-Key on every forwarded
    # request so upstreams can prove a request actually came through the gateway (ADR-018). ---
    INTERNAL_SERVICE_KEY: str = "dev-internal-key"

    # --- CORS (terminated once at the edge); comma-separated origins ---
    GATEWAY_CORS_ORIGINS: str = "*"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.GATEWAY_CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
