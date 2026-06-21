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

---

## RULE 3 — RUN THE E2E LOCALLY BEFORE OPENING THE PR

GitHub CI **cannot** run the deployed-stack e2e (it has no running stack). Therefore the e2e suite is run **locally against the deployed stack** before the PR is opened, and its PASS banner is **pasted into the PR body**. A behaviour-touching PR that does not carry a deployed-stack e2e PASS is **not ready** and must not be opened for review.

```
scripts/e2e.sh --up      # bring the stack up, then run tests/e2e -m e2e through the gateway
```

The suite **auto-skips** when the gateway (`:8006`) is unreachable — a "skip" is **not** a pass and does not satisfy this rule. Only a real green run counts.

---

## RULE 4 — THE CTO VERIFIES THE REAL E2E AT THE PR (CI-green is not enough to merge)

For any behaviour-touching backend PR, the **CTO agent must verify the real gateway/MCP e2e passes on the deployed stack before merging** — not merge on CI-green alone. CI-green + unit + testcontainers are necessary but never sufficient (Rule 1). The CTO either re-runs `scripts/e2e.sh` or confirms the PASS banner in the PR body against the current head. A PR without a verified real-e2e PASS does not merge.

**This is now mechanized: the CI `e2e` job (`.github/workflows/ci.yml`) builds + brings up the real stack and runs the gateway e2e on every PR** — deterministic + OAuth (real dex) keyless, plus the BYOM real-LLM leg when the `OPENROUTER_API_KEY` secret is set. So "the e2e is green" is a required check, not a manual local step. The local `scripts/e2e.sh` run (Rule 3) remains the fast pre-PR feedback loop; the CI job is the gate.

---

## RULE 5 — E2E IS THE END USER, THROUGH THE GATEWAY (nothing direct, nothing mocked, nothing assumed)

**Every e2e test simulates a real user's interaction through the application-gateway (`:8006`) — the only surface a real user touches.** This is the law for how every agent in this repo builds and reviews e2e tests:

- **Through the gateway only.** A real user never calls a service directly, so an e2e **never** hits a service port directly (`:8007`/`:8008`/…), and never an internal/`/internal` endpoint. It goes through `:8006` with a **real JWT from a real registration**.
- **The user brings their own everything, through the public APIs.** Their data, their model, their token — stored/configured via the real user-facing endpoints (e.g. `POST /credentials/` for BYOM), never injected server-side, never read from a config the user wouldn't control, never hardcoded in the test.
- **Nothing mocked, nothing assumed, nothing DB-direct.** No `FakeHarness`, no fake repos, no monkeypatch, no internal-function calls, no asserting against the database. Real services, real worker/broker, real harness. Faking a genuinely external third party (an OAuth provider, a model API) is replaced by a **real local server** (e.g. `dex`) or the user's **real sandbox credential** — not a mock.
- **End-user perspective only.** Assertions are on what the user observes through the API (status, response body, the run's state) — the things a real user would see.

A test that hits a service directly, mocks one of our services, or asserts against the DB is **not an e2e** and does not satisfy the Definition of Done. This understanding is part of the agent flow: a reviewer rejects any "e2e" that bypasses the gateway or fakes the platform.

---

## RULE 6 — CTO AND USE-CASE-GUARDIAN REVIEW START THE MOMENT THE PR IS CREATED

Every PR requires **two** reviews — the **CTO** (technical correctness) and the **use-case-guardian** (the use cases stay runnable). **Both start the instant the PR is opened, and run in parallel — with CI, and with each other.** A PR is never "open → wait for CI → then review"; it is **CI + CTO + use-case-guardian, all at once, from creation.**

- **Neither review waits on CI.** CI-green is a *merge* precondition (Rule 4), not a *review-start* precondition. A reviewer reads the diff and posts findings while CI is still running.
- **Neither review waits on the other**, or on any optional/adversarial/extra verification. Do not serialize the use-case-guardian behind the CTO, or either review behind a deeper check. An adversarial/extra pass runs *in parallel* and only ever gates the final merge, never the start of review.
- **Both must sign off before merge** (no merge on a single review); the PR author is never the sole approver. The use-case-guardian posts its approving review under the **`johnkennII`** identity (agents share the human account, so the author can't self-approve).
- The merge happens when **all three** are satisfied — CI green, CTO signed off, use-case-guardian approved — but all three were *in flight from the moment the PR opened*.
