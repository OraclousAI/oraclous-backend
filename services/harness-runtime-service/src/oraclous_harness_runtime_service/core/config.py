"""Service configuration (ORAA-4 §21 core layer) — env → Settings (harness runtime, R4).

The harness runtime is a Layer-3 interpreter: it loads an OHM, runs the agent tool-use loop, and
dispatches each capability invocation to the capability-registry over HTTP. It owns a small Postgres
store for its own execution rows + provenance sink. Identity follows the same seam as the other
services (gateway / dev / jwt). The dev organisation matches the capability-registry's dev org so a
standalone smoke's instances + credentials are visible across both. The upstream
URLs have container-network defaults; ``LLM_MODE`` is ``fake`` for key-free CI (a real BYOM provider
lands in slice 4).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HARNESS_", extra="ignore")

    # --- identity seam (ADR-018). `gateway`: trust the gateway's verified X-Principal-*/
    # X-Organisation-Id, gated by X-Internal-Key. `dev`: a fixed bearer for the standalone smoke.
    # `jwt`: decode a real auth-service token. `verify_token` keeps one signature for the swap. ---
    auth_mode: Literal["gateway", "dev", "jwt"] = "dev"
    dev_bearer: str = "dev-token"
    dev_user_id: str = "00000000-0000-0000-0000-0000000000e5"
    # matches capability-registry / KRS DEV_ORG_ID so a standalone smoke shares one tenant.
    dev_org_id: str = "00000000-0000-0000-0000-00000000050a"
    internal_service_key: str | None = None
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"

    # --- own store (Postgres): execution rows + the provenance sink. No hardcoded prod secret. ---
    database_url: str = "postgresql+asyncpg://oraclous:oraclous@postgres:5432/oraclous"

    # --- upstream services the runtime calls (over HTTP; never imported) ---
    capability_registry_url: str = "http://capability-registry-service:8000"
    credential_broker_url: str = "http://credential-broker-service:8000"
    knowledge_retriever_url: str = "http://knowledge-retriever-service:8000"

    # --- the LLM seam. `fake`: a deterministic, key-free responder (CI/smoke). Real protocol
    # shapes (native/openai-compatible/gemini-compatible) + BYOM creds land in slice 4. ---
    llm_mode: Literal["fake", "anthropic", "openai", "gemini"] = "fake"
    # hard cap on tool-use iterations (one LLM turn + its dispatches). Policy-tunable in slice 3.
    max_iterations: int = 6

    # --- OHM signature trust store: signer-id → public-key PEM. Set via HARNESS_OHM_TRUST_KEYS as a
    # JSON object. Empty by default (unsigned OHMs load; whether a signature is *required* is a
    # governance/policy decision in slice 3). ---
    ohm_trust_keys: dict[str, str] = {}

    @property
    def sync_database_url(self) -> str:
        """The synchronous psycopg DSN Alembic uses (swaps the asyncpg driver for psycopg)."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
