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
`ESCALATED` and captures the harness `assignment_id`.

## Task board (S4 — human resume)

`GET /v1/engine/tasks` is the open human task board: the org's `ESCALATED` jobs (each parked on a
harness assignment). `POST /v1/engine/tasks/{job_id}/complete` submits the human's output — the engine
calls the harness `POST /v1/harnesses/assignments/{id}/complete` over HTTP (which marks the assignment
COMPLETED and flips the parked harness run ESCALATED→SUCCEEDED with that output), then flips its own
job `ESCALATED→SUCCEEDED`. So a human-entrypoint OHM runs end to end: submit → ESCALATED on the board
→ the human completes it → both the harness run and the engine job are SUCCEEDED with the human output.

The worker (`tasks/run_tasks.py`) reconstructs the principal from the durable job's stored
`user_id`/`organisation_id`, binds the org context, forwards the same downstream identity to the
harness (ADR-018), and uses a NullPool engine disposed per task (ADR-012).

## Schedules (S5 — Celery Beat cron)

`POST /v1/engine/schedules` registers a durable schedule that fires a harness job (the OHM inline via
`manifest` or by registry id via `manifest_ref`); `GET` lists the org's schedules, `DELETE` removes
one. A **single Celery Beat process** (`execution-engine-beat`) ticks every minute and calls
`fire_due`: for each enabled `cron` schedule whose most-recent window hasn't fired, it creates a
QUEUED job — **idempotent** on the `engine_jobs (organisation_id, idempotency_key=schedule:window)`
unique constraint, so a duplicate tick never double-fires — enqueues it, advances `last_fired_at`,
and writes an `engine.schedule.fire` provenance event. The beat also drives the **S3 reaper**
(`engine.reap_stale`) every `reaper_tick_seconds`. Firing is at-least-once: a missed tick is a missed
fire, never a duplicate. (HA/leader-lock is deferred — exactly one beat must run.)

**Retry + timeout (S3):** a submit may declare `max_retries` and `timeout_seconds`. A `FAILED` or
`TIMED_OUT` attempt under its retry cap is automatically re-queued (`retry_count` increments, an
`engine.job.retry` provenance event is written) until the budget is spent. `timeout_seconds` is the
harness call's wall-clock — exceeding it marks the job `TIMED_OUT` (then retried if eligible).

**Durability semantics:** the queue is at-least-once with `task_acks_late` — a worker that dies before
committing `QUEUED→RUNNING` redelivers, and the CAS makes the re-run idempotent. A submit that can't
enqueue fails the row (`error_type=enqueue_failed`) rather than orphaning a phantom QUEUED job. A job
stuck `RUNNING` past `running_lease_seconds` (a worker/DB blip after RUNNING, no terminal checkpoint)
is timed out by the **reaper** (`engine.reap_stale` — the logic lands here; Celery Beat schedules it
in S5). `cancel` is best-effort on the record — it does not abort an in-flight harness run (the
harness keeps running; the engine job reflects the cancel).

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
