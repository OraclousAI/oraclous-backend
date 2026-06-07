# execution-engine-service

**Layer 3 (Harness Runtime + Execution Engine) · port 8008 · release R5.**

The execution engine is the **durable orchestration** layer above the synchronous harness-runtime: it
runs harnesses as background jobs, schedules them, manages the human task board, enforces timeout +
retry policy, and (later) resumes paused runs and coordinates round-tables. It **wraps the
harness-runtime over HTTP** — it never imports it (four-layer contract; both are Layer 3, so they talk
by API exactly as the harness calls the registry). Org-scoped (ADR-006), governed, and reached through
the gateway at `/v1/engine`.

## Slice 1 — durable job backbone (sync passthrough)

`POST /v1/engine/jobs` submits a durable harness job: the engine persists a `QUEUED` `engine_jobs`
row, calls the harness `POST /v1/harnesses/execute` over HTTP, maps the harness status onto the engine
state machine (`QUEUED → RUNNING → SUCCEEDED | FAILED | ESCALATED | TIMED_OUT | CANCELLED`), checkpoints
the terminal state, and writes a provenance event per transition. In S1 the run is **synchronous,
in-request** (S2 moves it to a Celery worker). `GET /v1/engine/jobs/{id}` + `GET /v1/engine/jobs`
read prior jobs (org-scoped).

A run that escalates to a human (`error_type=human_assignment`) parks the job `ESCALATED` and captures
the harness `assignment_id` — the seam the S4 task board resumes from.

## Identity

The gateway/dev/jwt seam mirrors the other services (ADR-018): in `gateway` mode the engine trusts the
gateway's verified `X-Principal-*`/`X-Organisation-Id` (gated on `X-Internal-Key`) and **forwards the
same identity to the harness** on every run, so org-scoping holds end-to-end.

## Store

Its own Postgres tables (`engine_jobs`, `engine_provenance`) with an independent Alembic lineage
(`alembic_version_execution_engine`) — the dev stack shares one Postgres across services.

## Smoke

`tests/smoke/smoke.sh` (gateway mode, key-free) brings up the stack and drives the engine **only
through the gateway**: a human-actor job → ESCALATED + a captured assignment; the PostgreSQL-Reader job
→ SUCCEEDED + a `harness_execution_id` (real tables, via the engine→harness→registry→Postgres chain);
read surfaces; edge-auth 401; provenance written.
