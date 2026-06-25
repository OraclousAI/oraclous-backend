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
- keyless harness: `HARNESS_LLM_MODE=fake` is the harness's deterministic mode for cheap CI/regression — a convenience gate, **NOT a valid Definition-of-Done proof**. A DoD proof that exercises a model uses a **real model via OpenRouter** (RULE 8); a `fake`-mode run is a mock and never proves a feature done.

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

For any behaviour-touching backend PR, the **CTO independently verifies the bound behaviour against the issue's Definition of Done — by driving the REAL remote deployed stack itself** (`http://<implementer-host>:8006`, currently `192.168.1.202:8006`) through the gateway, with real registration, real BYO credentials, and a **real model (RULE 8)** — before merging. The CTO does **NOT** merge on CI-green, does **NOT** rely on the implementer's pasted PASS, and does **NOT** run the implementer's test scripts (e.g. `scripts/e2e.sh`): it confirms the DoD with its **own** calls against the real stack. If anything fails it **raises it immediately** and blocks the merge; if the check needs a credential / token / repo the CTO lacks, it **asks the human** — never faking, skipping, or working around it. CI-green + unit + testcontainers + the implementer's word are necessary but never sufficient (Rule 1). A passing run that used stale images (RULE 7) or a fake LLM (RULE 8) is **deception**, not a proof, and the PR does not merge.

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

---

## RULE 7 — EVERY PUSHED IMPLEMENTATION BRANCH IS REBUILT AND LEFT RUNNING ON THE REMOTE (no stale images)

The moment an implementation branch is pushed, the implementer **rebuilds the FULL deployed stack from that branch on the implementer machine** and leaves it running — so the remote tester (the CTO / Reza) tests an **already-built, already-serving** stack and never builds anything.

```
docker compose --env-file deploy/.env -f deploy/docker-compose.yml -f deploy/docker-compose.dev-ports.yml up -d --build --wait
```

- **`--build` is mandatory.** `up` (or `--force-recreate`) **without** `--build` runs the previously-built, **stale** image — the running container does not carry the branch's source. `scripts/e2e.sh`'s `--up`/recreate paths omit `--build`, so they do **not** satisfy this; the rebuild is explicit and full (every service, that branch's source), every service left **Healthy** (`up --wait` blocks until so).
- **A pushed branch whose stack is stale or unbuilt is NOT done** — the same failure class as red CI: the next person cannot actually test the new code.
- **The remote user only tests.** Pull the branch → it is already built and serving on the known ports (gateway `:8006`). "It builds from source" is the implementer's job to do here, on push — never an excuse to leave the remote user to build.

---

## RULE 8 — NO FAKE LLM IN A DEFINITION-OF-DONE PROOF (real OpenRouter, always)

Any Definition-of-Done proof that exercises a model uses the **real LLM via OpenRouter** — BYOM, the user's key **configured through the gateway** (`POST /credentials/`), resolved by the broker, nothing injected server-side. **A fake / scripted LLM is a mock.** `HARNESS_LLM_MODE=fake` scripts the model's output; a run that used it is **not** a valid proof of done — the same failure class as a mock, a stale image (RULE 7), or red CI.

- **The deterministic fake-LLM suite is a cheap CI / regression gate ONLY.** It never stands in for the real-model proof, and a green fake-LLM run is never "done."
- **If a feature exercises an agent / LLM, its DoD is shown with a REAL model, or it is not done.** If it should be real, it is real — every test, every capability, the real thing. Anything that needs a model goes to OpenRouter; anything that needs a credential / token / repo is **asked of the human**, never faked or worked around.
- A passing e2e that used a fake LLM is **deception**, not a proof — it is rejected at review and the work is not done.
