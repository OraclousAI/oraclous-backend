"""Service configuration (ORAA-4 §21 core layer) — env → Settings (execution engine, R5).

The execution engine is a Layer-3 orchestrator: it runs harnesses as durable jobs, schedules them,
manages the human task board, and resumes paused runs — calling the harness-runtime over HTTP (never
importing it; four-layer contract). It owns a small Postgres store (job rows + provenance sink) +
Redis-backed Celery queue (worker/beat land in later slices). Identity follows the same seam as the
other services (gateway / dev / jwt); the resolved principal is forwarded downstream to the harness
(ADR-018) so org-scoping holds end-to-end.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENGINE_", extra="ignore")

    # --- identity seam (ADR-018). `gateway`: trust the gateway's verified X-Principal-*/
    # X-Organisation-Id, gated by X-Internal-Key. `dev`: a fixed bearer for the standalone smoke.
    # `jwt`: decode a real auth-service token. `verify_token` keeps one signature for the swap. ---
    auth_mode: Literal["gateway", "dev", "jwt"] = "dev"
    dev_bearer: str = "dev-token"
    dev_user_id: str = "00000000-0000-0000-0000-0000000000e7"
    # matches the other services' DEV_ORG_ID so a standalone smoke shares one tenant.
    dev_org_id: str = "00000000-0000-0000-0000-00000000050a"
    internal_service_key: str | None = None
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"

    # --- own store (Postgres): job rows + the provenance sink. No hardcoded prod secret. ---
    # The ORG-BOUND engine DSN (ADR-030 §3): the request/driver path + the org-bound Celery task
    # execution connect here as the NOSUPERUSER ``oraclous_app`` role so the FORCE'd RLS policy
    # bites. Migrations, the rls-role bootstrap, AND the cross-org maintenance reader use the
    # ``maintenance_database_url`` (the owner) instead — see below.
    database_url: str = "postgresql+asyncpg://oraclous:oraclous@postgres:5432/oraclous"

    # --- the MAINTENANCE engine DSN (ADR-030 §3 carve-out, mirrors auth's owner-engine split). The
    # cross-org sweeps — the reaper (list_stale_running) and Beat (list_enabled_cron) — read ACROSS
    # orgs with NO bound org, so they MUST run on a role that bypasses RLS (the owner/superuser),
    # else FORCE'd RLS fails them closed to zero rows and a dead worker's job / a due cron is never
    # found. Defaults to the OWNER DSN; in the deployed stack the org-bound `database_url` flips to
    # oraclous_app while this stays the owner. A self-host could instead point this at a dedicated
    # BYPASSRLS role. The per-row settle AFTER a sweep still goes through the org-bound engine with
    # the row's own org bound (org_scope), so only the cross-org READ uses this engine.
    maintenance_database_url: str | None = None

    # --- Postgres RLS backstop (ADR-030 / #353) ---
    # When true, the service asserts at startup (web lifespan) AND the worker asserts at
    # worker_process_init that the ORG-BOUND runtime DB role is NOSUPERUSER/NOBYPASSRLS (a bypassing
    # role silently voids the RLS policy — T1-M3) and FAILS CLOSED otherwise. The deployed api +
    # the org-bound worker path connect as oraclous_app with this on; migrations, the rls-role
    # bootstrap, and the maintenance/reaper/beat engine keep running as the owner and never set it.
    # Default false so a test/local run that intentionally uses the owner DSN is not forced to
    # provision the app role.
    rls_assert_runtime_role: bool = False

    # --- the durable queue (Celery over Redis; the worker runs jobs out-of-request). Redis DB **1**
    # isolates the engine's broker + result backend from the knowledge-graph worker on db 0 — they
    # share the redis instance, and the default `celery` queue would otherwise cross-deliver tasks
    # (the KGS worker would reject `engine.run_job` as unregistered, silently dropping the job). ---
    redis_url: str = "redis://redis:6379/1"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    # --- upstream the engine calls (over HTTP; never imported) ---
    harness_runtime_url: str = "http://harness-runtime-service:8000"
    # an out-of-request harness run can be long (an LLM loop) — generous default.
    harness_request_timeout: float = 600.0
    # the knowledge-retriever hosts core/evaluate (the flow judge) — the engine grades a completed
    # team run at the gate (ADR-037 / #477). Bounded UNDER the harness budget; the judge's own
    # 25s deadline returns partial rather than 504-burning (ADR-037 Decision 5).
    knowledge_retriever_url: str = "http://knowledge-retriever-service:8000"
    evaluate_request_timeout: float = 35.0
    # the reaper times out a job stuck RUNNING longer than this (worker/DB blip after RUNNING). Set
    # ABOVE the Celery hard limit (3600s) so a healthy long run is never falsely reaped.
    running_lease_seconds: int = 3900
    # Celery Beat cadences (S5): fire due cron schedules every minute; sweep stranded RUNNING jobs.
    schedule_tick_seconds: float = 60.0
    reaper_tick_seconds: float = 300.0

    @property
    def celery_broker(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def celery_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    @property
    def sync_database_url(self) -> str:
        """The synchronous psycopg DSN Alembic + the rls-role bootstrap use (swaps the asyncpg
        driver for psycopg). Always the OWNER DSN — migrations + bootstrap are owner privileges."""
        return self.maintenance_url.replace("+asyncpg", "+psycopg")

    @property
    def maintenance_url(self) -> str:
        """The MAINTENANCE (owner / BYPASSRLS) async DSN the cross-org sweeps read on (ADR-030 §3).

        Defaults to ``database_url`` so a single-DSN deploy/test (no RLS split) behaves exactly as
        before — both engines are the owner and RLS is a no-op. In the deployed RLS stack
        ``database_url`` flips to the org-bound oraclous_app role while ``maintenance_database_url``
        stays the owner, so only the cross-org reader bypasses RLS. Alembic + the bootstrap derive
        their owner DSN from this too (``sync_database_url``)."""
        return self.maintenance_database_url or self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
