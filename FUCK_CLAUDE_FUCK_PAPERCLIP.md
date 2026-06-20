# FUCK_CLAUDE_FUCK_PAPERCLIP

The canonical rules for this repo live **HERE**, in the repo, in git — not in Paperclip, not in ORAA, not in any external tracker or agent bundle. When anything disagrees with this file, **this file wins.**

---

## RULE 1 — DEPLOYED-STACK TESTING IS MANDATORY (do not bypass)

A feature is **NOT tested and NOT done** until it has been driven against the **DEPLOYED docker stack** — the built images, the real services, the real Celery worker + broker, the real harness — through its **real HTTP API endpoints** (or a real MCP server).

**Necessary but NOT sufficient, and never a substitute:**
- CI-green (ruff / mypy / unit).
- testcontainers integration tests (a real DB but `FakeHarness` / fake repositories / mocked seams) — this is a *hypothesised* version, not the deployed one.
- calling internal functions, monkeypatching, or asserting against the database directly.

**Forbidden in an end-to-end / acceptance test:** fakes, mocks, custom backend logic standing in for a real service, internal-function calls, DB-direct assertions.

**The acceptance bar:**
1. Rebuild the changed images from current `main` (`docker compose -f deploy/docker-compose.yml build <svc>`).
2. Recreate the services (`... -f deploy/docker-compose.dev-ports.yml up -d <svc>`), wait healthy.
3. Prove the bound behaviour with real HTTP calls (`curl` / `httpx`) against the live endpoints.

**Why:** the team-runtime (E1/E2/E3) shipped CI-green on a stack that was 2 days stale; the full engine↔worker↔harness HTTP wiring, the broker, and the registry seed were never exercised end-to-end. CI-green ≠ runs-deployed. The real-stack run also surfaced bugs CI never could (engine in `gateway` auth-mode needing `X-Internal-Key` + `X-Principal-*` headers, not a bearer; a precedence parser that only stripped `←`, not `<-`).

**Deployed-stack facts (this stack):**
- engine on host `:8008`, harness `:8007` (via `deploy/docker-compose.dev-ports.yml`).
- engine `ENGINE_AUTH_MODE=gateway` → send headers `X-Internal-Key: dev-internal-key`, `X-Principal-Id: <uuid>`, `X-Principal-Type: user`, `X-Organisation-Id: 00000000-0000-0000-0000-00000000050a` (NOT a bearer).
- keyless harness: `HARNESS_LLM_MODE=fake` (the harness's own deterministic mode — a real service config, not a test mock).

---

## RULE 2 — THE RULES LIVE IN GIT, NOT IN PAPERCLIP/ORAA

Governance for this repo is this file + `CLAUDE.md`, both checked into the repo. Paperclip / ORAA / external agent bundles are not the source of truth and are being removed. Do not add new pointers to them.
