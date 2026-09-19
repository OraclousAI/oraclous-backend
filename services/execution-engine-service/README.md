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

## Team runs — the platform saves a member's deliverable (#1137)

On a **graph-bound** team run, the engine writes each member's declared deliverable onto the run's
knowledge graph **itself**, at the moment the member settles. It does not wait for the model to
call a `graph-ingest` tool: a bound save tool is a menu, not an intent, and a member that simply
never called it used to finish successfully with its answer stored on the run and nothing on the
graph at all.

A member is saved when **all** of these hold: the run is bound to a graph; the member settled
`succeeded` or `partial` with a real result; its manifest **declared** required output keys
(`outputs_schema.required`); and it actually delivered every key it declared. The document is the
canonical JSON of exactly those declared keys, always ingested as **text** (never a structured
type), with no title — the graph names it after the producing member's role. Its producer stamp
carries the same `team_run_id`, `member_role`, `team_id` and harness `execution_id` the member's
own tool-written documents carry, so both land under one execution.

Before writing, the engine lists that run's artifacts for that member role once and **skips** if
any row came back in a state other than `failed` — which covers both a model save that really
landed and the engine's own earlier write. If that listing itself fails, the write proceeds: a
duplicate document is recoverable, a lost deliverable is not.

**Known limits, by design:**

- a **fan-out** member is not saved. Its sub-runs share one role and the per-item ordinal that
  would tell their documents apart is not recoverable at settle (#1015).
- a **re-drive** that settles the same member again while an earlier document exists stays
  suppressed by the duplicate check: the graph keeps the first drive's document, not the newest.
- a member that settles **twice within one drive** — a loop member going `partial` then
  `succeeded`, or a recalibration retry — is suppressed the same way, so the graph can keep the
  earlier, weaker answer and drop the final one. Known, not yet fixed: the duplicate check would
  have to compare the new document against the existing row's content rather than treat any
  non-`failed` row as a save.

The write is **best-effort and never fails a settled member** — an unreachable or rejecting
knowledge-graph-service is logged (`platform member-artifact save failed (best-effort)`) and the
run continues. Every attempted write emits an `engine.team_run.artifact` provenance event
(`saved` / `failed`, carrying the settle's output hash), so what the platform wrote is auditable
without reading the graph.

It never **delays** one either. Both calls carry their own short deadline,
`ENGINE_ARTIFACT_SAVE_TIMEOUT_SECONDS` (default `5`), rather than the engine's 30s
knowledge-graph client default — a degraded graph service must not add a minute of wall clock to
every settled member. Blowing the deadline is a logged skip, not an error: a listing that times
out abandons the save outright (`member artifact save skipped, duplicate check timed out`), and an
ingest that times out records `failed` because its outcome is genuinely unknown (`member artifact
save timed out … (outcome unknown)`). Lower the value if your graph service is slow; the cost of
lowering it is a missing document, which the member's own answer on the run still survives.

## Build runs — the platform saves the compiled team (#1169)

When a build (compiler) run settles `SUCCEEDED`, the worker saves the compiled team as a draft
itself. A retry from the run page, a closed console tab, or a build started through the gateway API
without the console all leave a saved team. The save is **best-effort**: a failed or slow save never
changes the run's result, and is logged, not raised.

`POST /v1/engine/team-drafts/from-run` stays as the safety net and saves the team if the settle-time
save did not happen. It is one draft per run, so when the settle-time save got there first it
returns that draft with HTTP `200` instead of `201`. Callers must accept both. The settle-time save
names the draft `compiled-<first 8 hex chars of the run id>`; a later `from-run` call does not
rename it. Runs that finished before this change are not backfilled; `from-run` still saves them on
demand.

The save has its own deadline, `ENGINE_TEAM_DRAFT_SAVE_TIMEOUT_SECONDS` (default `60`, seconds).

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
