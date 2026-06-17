"""Service configuration (ORAA-4 §21 core layer) — env → Settings (harness runtime, R4).

The harness runtime is a Layer-3 interpreter: it loads an OHM, runs the agent tool-use loop, and
dispatches each capability invocation to the capability-registry over HTTP. It owns a small Postgres
store for its own execution rows + provenance sink. Identity follows the same seam as the other
services (gateway / dev / jwt). The dev organisation matches the capability-registry's dev org so a
standalone smoke's instances + credentials are visible across both. The upstream
URLs have container-network defaults; ``llm_mode`` defaults to ``live`` (fail-closed, ADR-021 §1) —
a real BYOM client per execution; ``fake`` (key-free) is the EXPLICIT dev/CI/smoke selection.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Default OpenAI-compatible base URLs (also the fallback when the env supplies a blank override).
_DEFAULT_LLM_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
}


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

    # ADR-030 §3: when True the lifespan asserts at startup that the runtime DB role is
    # NOSUPERUSER/NOBYPASSRLS (else the RLS backstop is inert — T1-M3) and refuses to come up
    # otherwise. The deployed runtime (oraclous_app DSN) sets it (HARNESS_RLS_ASSERT_RUNTIME_ROLE);
    # a deliberate owner-DSN dev/test run leaves it False (the default), so importing this module
    # never forces the assertion.
    rls_assert_runtime_role: bool = False

    # --- upstream services the runtime calls (over HTTP; never imported) ---
    capability_registry_url: str = "http://capability-registry-service:8000"
    credential_broker_url: str = "http://credential-broker-service:8000"
    knowledge_retriever_url: str = "http://knowledge-retriever-service:8000"
    knowledge_graph_url: str = "http://knowledge-graph-service:8000"

    # --- post-run agent-memory hook (#332 / ADR-027 §5). DEFAULT FALSE IN CODE (the zero-risk
    # constraint): with the flag off the memory writer is never constructed and zero memory calls
    # happen — existing runs carry zero new risk. The deploy env opts in (HARNESS_MEMORY_WRITES=
    # true in deploy/.env). Writes are fire-and-forget with this short timeout; every failure is
    # swallowed + logged — a memory write can NEVER fail, block, or slow a run. ---
    memory_writes: bool = False
    memory_write_timeout: float = 2.0
    # Bounded grace, on shutdown only, for in-flight memory writes to land before teardown cancels
    # them. SMALL — it must never appreciably delay shutdown; the writes are best-effort regardless.
    memory_drain_timeout: float = 2.0

    # --- the LLM seam. `live` (fail-closed default, ADR-021 §1): a real client from the OHM model's
    # protocol_shape + a per-execution BYOM key via the broker (ADR-008; the harness never holds a
    # model key in its own env) — a deploy that forgets the override runs the real LLM, never a
    # scripted one by accident. `fake`: key-free deterministic responder, valid for CI/smoke but
    # selected EXPLICITLY (compose dev profile + CI); selecting it fires a loud one-time startup
    # alert at the selection site (core/lifespan.py). ---
    llm_mode: Literal["fake", "live"] = "live"
    # provider (the first segment of an OHM model binding) → OpenAI-compatible base URL. OpenRouter
    # serves Claude/OpenAI/Gemini/etc. behind one OpenAI-compatible endpoint + one key.
    llm_base_urls: Annotated[dict[str, str], NoDecode] = dict(_DEFAULT_LLM_BASE_URLS)
    # A custom BYOM connection may carry its own `base_url` (any OpenAI-compatible endpoint). That
    # URL is user-controllable, so it is run through the egress guard (domain/llm/egress.py). True
    # (single-tenant default) lets a user's local LLM work (host.docker.internal / 127.0.0.1 /
    # 192.168.x); the link-local/cloud-metadata range stays blocked regardless. A MULTI-TENANT
    # deployment MUST set this False so one tenant can't reach loopback/RFC-1918 internal services.
    allow_private_llm_targets: bool = True
    llm_request_timeout: float = 120.0
    # Safety backstop on tool-use iterations (one LLM turn + its dispatches). The per-tier tool-call
    # budget (policy set) is the real governance limit and binds within this cap.
    max_iterations: int = 25

    # --- OHM signature trust store: signer-id → public-key PEM, via HARNESS_OHM_TRUST_KEYS (JSON).
    # Empty by default; a *required* signature comes from the policy set or the flag below. ---
    ohm_trust_keys: Annotated[dict[str, str], NoDecode] = {}
    # When true, an unsigned OHM is rejected (closes signature-stripping). ORed with the resolved
    # policy set's require_signature — fail-closed: either source requiring it makes it required.
    ohm_require_signature: bool = False
    # Deployment governance floor: when set, this policy set is FORCED for every run, ignoring the
    # manifest's policy_set_ref — so an author can't self-select a weaker tier. Unset → the author's
    # referenced set (defaulting to development-default) applies.
    force_policy_set: str | None = None

    @field_validator("ohm_trust_keys", "llm_base_urls", mode="before")
    @classmethod
    def _blank_or_json_dict(cls, v: object, info: object) -> object:
        """Tolerate a blank env override (compose ``${VAR:-}`` yields an empty string).

        With NoDecode the env source hands us the raw string: blank → the field default; non-blank →
        parsed JSON. Prevents a SettingsError crash on startup when the var is set-but-empty.
        """
        if isinstance(v, str):
            if not v.strip():
                # mypy: info is a FieldValidationInfo; only used for the field name here.
                return _DEFAULT_LLM_BASE_URLS if info.field_name == "llm_base_urls" else {}  # type: ignore[attr-defined]
            return json.loads(v)
        return v

    @property
    def sync_database_url(self) -> str:
        """The synchronous psycopg DSN Alembic uses (swaps the asyncpg driver for psycopg)."""
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
