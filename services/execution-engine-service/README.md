# execution-engine-service

**Layer 3 (Harness Runtime + Execution Engine) · port 8008 · release R5.**

The execution engine is the **durable orchestration** layer above the synchronous harness-runtime: it
runs harnesses as background jobs, schedules them, manages the human task board, enforces timeout +
retry policy, and (later) resumes paused runs and coordinates round-tables. It **wraps the
harness-runtime over HTTP** — it never imports it (four-layer contract; both are Layer 3, so they talk
by API exactly as the harness calls the registry). Org-scoped (ADR-006), governed, and reached through
the gateway at `/v1/engine`.

## Durable jobs (async)

`POST /v1/engine/jobs` accepts a durable harness job (**202**): the engine persists a `QUEUED`
`engine_jobs` row and enqueues it on Redis; a **Celery worker** then calls the harness `POST
/v1/harnesses/execute` over HTTP, maps the harness status onto the engine state machine (`QUEUED →
RUNNING → SUCCEEDED | FAILED | ESCALATED | TIMED_OUT | CANCELLED`), and checkpoints the terminal state
with a provenance event per transition. Poll `GET /v1/engine/jobs/{id}` for the outcome; `GET
/v1/engine/jobs` lists the org's jobs. `POST /v1/engine/jobs/{id}/cancel` cancels a
QUEUED/RUNNING/ESCALATED job.

Every state change is a **CAS transition under a row lock** (`JobRepository.transition`), so a
concurrent cancel can never race the worker — `can_transition`/`sources_for` (domain/state.py) define
the only legal moves. A run that escalates to a human (`error_type=human_assignment`) parks the job
`ESCALATED` and captures the harness `assignment_id` — the seam the S4 task board resumes from.

The worker (`tasks/run_tasks.py`) reconstructs the principal from the durable job's stored
`user_id`/`organisation_id`, binds the org context, forwards the same downstream identity to the
harness (ADR-018), and uses a NullPool engine disposed per task (ADR-012).

**Durability semantics (S2):** the queue is at-least-once with `task_acks_late` — a worker that dies
before committing `QUEUED→RUNNING` redelivers, and the CAS makes the re-run idempotent. A submit that
can't enqueue fails the row (`error_type=enqueue_failed`) rather than orphaning a phantom QUEUED job.
Two gaps close in **S3**: a job stuck `RUNNING` (worker/DB blip after RUNNING, no terminal checkpoint)
is reaped by a lease sweep, and `cancel` is best-effort on the record — it does not abort an in-flight
harness run (the harness keeps running; the engine job reflects the cancel).

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
