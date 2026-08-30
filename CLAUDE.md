# CLAUDE.md — oraclous-backend

This file is the working contract for any AI agent (Claude Code, an agent in the harness runtime, or otherwise) operating in this repository. Read it in full at the start of every session. This repo is `OraclousAI/oraclous-backend` — the Python codebase for the Oraclous Platform: a working 8-service platform, each service under `services/<service>/`, layered `routes → services → domain → repositories → core`. Each service directory carries its own `services/<service>/CLAUDE.md` (layer, target shape, reference page) — it loads automatically when you work in that directory; consult the service's reference page before touching its directory. Work is tracked as GitHub Issues + PRs in this repo, driven via the `gh` CLI; agents pick up issues by assignee/label.

## 0. The rules live in git

<!-- #665 stage 4: the R1–R8 précis here duplicated the canonical file + §9 (law kept verbatim there); R6/R7 had no other home and moved to Governance gates at full strength. -->
The canonical rules are `FUCK_CLAUDE_FUCK_PAPERCLIP.md` (repo root, in git) plus this file — there is no external operating contract, agent bundle, or tracker (paperclip/ORAA are removed). When anything disagrees with `FUCK_CLAUDE_FUCK_PAPERCLIP.md`, that file wins; when this file disagrees with shipped reality, fix this file. Its rules R1/R3/R4/R5/R8 (deployed-stack e2e through the gateway; run it locally pre-PR; CTO-verified on the real stack; no fake LLM in a done-proof) are restated at full strength in §9; R6 and R7 are in the Governance gates below. Read the canonical file itself before your first PR on an issue.

## Governance gates

The rules that bite most. Hook-enforced locally: `.githooks/pre-push` (§4.7) + `.githooks/commit-msg` (§4.5), wired via `core.hooksPath=.githooks`. Server-side: `main` is protected by a GitHub ruleset (public repo, no admin bypass) — required CI checks + a non-author approving review + up-to-date base; the CTO merges via `oraclous-knowledge/operations/gated_merge.sh` (§8).

- **PR-BUNDLING LAW (non-negotiable).** Never ship a one-commit-per-PR stream. "One commit per concern" means multiple commits inside ONE PR, NOT one PR per commit. Bundle related concerns into a single PR — CI (~6 min) + non-author review + redeploy run once per PR, so a separate PR per commit multiplies the cost. An issue with N sub-tasks ships as one PR with N commits, never N PRs (e.g. a mypy + OTel + Celery issue = one PR / three commits). Default to fewer, bigger PRs; the only exception is changes in different repos (which can't share a PR).
- **Pre-open readiness.** Before OPENING a PR for review it must be pre-push-clean, CI-green, and rebased onto current `main` (not BEHIND). You own this; a reviewer never discovers red CI or a needed rebase.
- **Branch-from-merged-tests.** An `[impl]` PR branches from / rebases onto the commit where its `[tests]` PR merged, before opening — this kills add/add conflicts and preserves ADR-010 two-PR independence.
- **Reviews start at PR creation (R6).** The CTO and use-case-guardian reviews start the moment a PR is created, in parallel with CI and each other.
- **Rebuild on push (R7).** Every pushed implementation branch is rebuilt (`up -d --build --wait`, every service) and left Healthy on the implementer machine so the remote tester never runs a stale image.
- **Small conflicts fold in.** Conflicts/misalignments discovered mid-issue are folded into the current PR, not spawned as new tickets.
- **Docker.** Multi-service functionality is `docker-required`; run its integration tests on Docker. If the daemon is down, raise an error and block `needs-human` — never skip.
- **Canonical service architecture (R3.5).** Every service follows the layered structure `routes → services → domain → repositories → core` (package root `src/oraclous_<svc>_service/`). No business logic, no DB drivers, and no non-`BaseModel` class defs in `routes/`; repositories are the ONLY DB/Neo4j/Redis access. Enforced by `tools/lint/check_service_structure.py` + `check_no_stubs.py` + the root `pyproject.toml` `[tool.importlinter]` contracts (CI `lint` + pre-push). Standard: `oraclous-knowledge/engineering/service-architecture-standard.md`.
- **Hardened per-service DoD (R3.5).** A SERVICE is done only by 8 gates: structure + not-hollow (`check_no_stubs` zero findings; flip `tools/lint/service_status.yaml`) + runs (`docker compose up` healthy) + real endpoints (integration vs real substrate) + smoke vs real substrate (`smoke.sh`, the `r3_5_gate` CI job) + Reza sign-off (`needs-human`). A stub never passes done.
- **R3.5 delivery.** Active release: rebuild every service real, per service, in ≤6 coarse vertical slices (no micro-tickets). Spec = legacy `develop@84152635` (`git show develop:<path>`; never write `legacy-reference`). Old R4–R8 roadmap discarded.
- **KB currency.** If you change `oraclous-knowledge`, keep the docs current and refresh graphify in the same change.

## 1. Identity and scope

This is the backend execution repository. Personas acting here: `backend-implementer` (all production Python code, `[impl]` PRs), `test-author` (tests *before* implementation, `[tests]` PRs), `be-test-reviewer` (Tests Review gate — the narrow BE-only architecture+security verification persona), `code-reviewer` (craft review, always on every `[impl]` PR), `qa-engineer` (test suite, coverage, flakiness; regression tests under `tests/`).

The CTO agent holds full technical authority over this repo: it signs off final gates, merges feature PRs, accepts ADRs, and approves architecture/release changes. It escalates to the human (Reza Jahankohan) only when something is ambiguous, blocked, or out-of-policy (§8).

Planning, architecture, cross-cutting agreement, infra, and docs personas (`product-planner`, `solution-architect`, `security-architect`, `devops-implementer`, `docs-writer`) live in the coordinator session at the workspace root, not here. You receive ready, briefed issues with lift-tags from them via GitHub issue assignment; when this session needs an architecture decision, a Contract, a brief fix, threat tagging, infra, or a doc change, it escalates by reassigning the issue to the relevant coordinator persona — it does not load that persona here. The one apparent exception: the Tests Review gate is `be-test-reviewer` (which lives here), not the architects — it verifies tests against already-made decisions and escalates decision-level problems up. Residency map + skill catalogue: `docs/knowledge-links.md`; read your own skill page on session start.

## 2. Source of truth

**The `oraclous-knowledge` git repository is canonical** for architecture, ADRs, governance, and engineering process. Confluence is a read-only mirror — when it disagrees with `oraclous-knowledge` or with shipped reality, the knowledge repo wins; never edit the mirror. When this file disagrees with the knowledge base, the knowledge base wins — open a `docs-writer` issue to reconcile. Consult the knowledge base before the web or your training; when a page is stale (shipped reality has moved past it), open a `docs-writer` issue rather than editing architecture/ADR pages directly. Page index: [`docs/knowledge-links.md`](docs/knowledge-links.md). The master board is GitHub Issues + PRs (Goals → Projects → Issues), nothing else; your work is whatever is assigned to you on GitHub (§5).

## 3. Architecture invariants

Non-negotiable: a PR that violates any of these is rejected at review regardless of how well the tests pass. ADR links: the ADR index in `docs/knowledge-links.md`.

<!-- #665 stage 4: invariants compressed to one statement + ref each; 3.3's as-built RLS detail moved verbatim to the path-scoped .claude/rules/tenancy-rls.md. -->
- **3.1 Four layers, imports downward only** (ADR-001). Layer 4 Application Gateway (`application-gateway-service`) → Layer 3 Harness Runtime + Engine (`harness-runtime-service`, `execution-engine-service`) → Layer 2 Capability Registry (`capability-registry-service`) → Layer 1 Substrate (`auth-service`, `credential-broker-service`, `knowledge-graph-service`, `knowledge-retriever-service`). Substrate never imports from above; each layer imports only downward; no service has its own database access bypassing the Substrate primitives. Enforced by the root `pyproject.toml` import-linter contracts (CI `lint` + pre-push).
- **3.2 OHM is the canonical manifest format** (ADR-002, OHM v1.0 spec). Every harness, capability descriptor, and policy set is OHM; conversion to/from external formats (Claude Code skills, LangGraph, Codex agents) happens at adapter boundaries only.
- **3.3 `organisation_id` on every storage operation** (ADR-006, ADR-012, ADR-030). Every Substrate write carries `organisation_id`; every read is parameterised by it; there is no code path without it. App-layer org-scoping is the primary, live tenancy control; the Postgres RLS backstop is realized as the defense-in-depth second line. Tenant-scoped access goes through the `oraclous_substrate.access` seam (`scoped_*`), which fails closed when no org is bound — bind the org on every request-path DB op. As-built detail (roles, org-GUC, forced-RLS tables, guardrails): `.claude/rules/tenancy-rls.md`.
- **3.4 ReBAC mediates every cross-organisation traversal** (ADR-004). Any operation touching data of an organisation other than the actor's home organisation calls the Substrate's access decision API first; direct database queries that bypass ReBAC are forbidden.
- **3.5 Fail-closed defaults.** Ambiguous authorisation → deny; content-hash mismatch → reject; failed budget check → halt. There is no "if in doubt, allow" path anywhere.
- **3.6 Operator separation in cloud-hosted mode** (ADR-008). Oraclous-the-company staff cannot decrypt customer BYOM credentials or customer data; the KMS envelope is held outside Oraclous's control. Code that would weaken this — for any reason, including "for support" or "for debugging" — is rejected.
- **3.7 Provenance on every capability invocation** (Governance Model §6, threat T7). Every capability dispatch produces a provenance record, written through the runtime's single collector, never direct database writes.
- **3.8 Harnesses are descriptors, not code** (ADR-003, ADR-005). A harness is an OHM manifest the runtime *interprets* — never a Python class, never compiled into platform code. The compiler harness and consciousness skills are themselves harnesses.

## 4. Working agreement

### 4.1 TDD is the contract (ADR-010)

1. `test-author` opens a `[tests]` PR with failing tests; `be-test-reviewer` reviews it at the Tests Review gate; it merges.
2. `backend-implementer` opens an `[impl]` PR with the minimum code that turns the failing tests green.
3. `code-reviewer` (always), `qa-engineer` (always), and any architects whose surfaces are touched review it; the CTO gives final sign-off and merges.

The implementer **never** modifies tests to make them pass. If a test is wrong, that is a discovery: flag it to `test-author` with the specific reason and propose a corrected test.

Tests that need a not-yet-built intra-repo seam (`oraclous_*`) import it function-locally, never at module level — and never convert the missing seam into a skip; it must hard-fail RED until the `[impl]` lands. Full rule + rationale: `.claude/rules/tests-seam-imports.md`. Enforced by the `check_test_imports` guardrail (TST001/TST002) and the pre-push `pytest --collect-only` check (§4.7).

### 4.2 PR naming, sizing, branches

| Prefix | Meaning | Author |
| --- | --- | --- |
| `[tests]` | Tests-only PR (failing tests, no implementation) | `test-author` |
| `[impl]` | Implementation PR against merged tests | `backend-implementer` |
| `[impl-infra]` | Infrastructure changes (Docker, compose, Helm, workflows) | `devops-implementer` |
| `[regression]` | Regression test for a discovered bug | `qa-engineer` |
| `[docs]` | Repo-level docs (this file, READMEs) | `docs-writer` |
| `[chore]` | Dependency bumps, version pins, formatting passes that don't touch behaviour | any implementer |

`[spike]` marks explicit prototype/exploratory work outside TDD (marked on the issue too); spikes never merge to `main` — they produce findings that feed a normal TDD issue.

Target under 300 net lines per PR; justify overruns in the description, or request a split before opening. `main` is protected; no direct pushes. Branches are `<agent-name>/<issue>-<slug>`, e.g. `backend-implementer/178-organisation-id-on-substrate-writes`; the issue identifier is the GitHub issue number.

### 4.5 Commits

First line `[#<issue>] [agent:NAME] Short imperative description` (longer body optional). The agent prefix is how the audit trail attributes work to agents, since all agents share the human GitHub account. One commit per concern — never bundle unrelated changes into a single commit. Forbidden in any commit message, PR body, review, or comment: `Co-Authored-By` in any variant, "Generated with"/"Generated by", `claude.ai`, any Anthropic attribution, and the robot emoji. Both the forbidden list and the first-line format (`[#<issue>]` ref(s) + at least one `[agent:NAME]`/`[area]` tag; merge/revert/fixup commits exempt) are enforced by `.githooks/commit-msg`.

### 4.7 Mandatory local pre-push gate

<!-- #665 stage 3: pytest --collect-only moved INTO the hook, proven to block first. Stage 4: §10's "own fix, never a [fix] issue" restatement deduped into this stronger one. -->
The wired `.githooks/pre-push` hook runs the CI `lint` job's static checks (ruff check/format, mypy, import contracts, and the full guardrail suite) plus `uv run pytest --collect-only` on every push and blocks a failing one. A push that fails is the implementer's own responsibility to fix before re-pushing — it does not become a separate `[fix]` issue. Bypassing the hook (`git push --no-verify`) is a violation except for the one-time hook-bootstrap commit.

### 4.8 Workspace discipline

Per-run git worktrees are currently OFF: every agent that writes this repo shares one checkout. Writer runs operate with `maxConcurrentRuns=1` (the CTO must not route two concurrent write-tasks here), start clean (check out the intended base before working), end clean (commit and push everything; never leave uncommitted changes), and serialize same-repo work via issue blocking.

## 5. Agent identity and the board

Agent identity is GitHub issue assignment — the assignee *is* the acting persona and owns the issue. Your work is the set of issues assigned to you; on pickup, read the issue and its comments first — the last `[agent:NAME]` comment with an action trailer tells you where the work stands.

The `needs-human` label is the controlled signal that an issue is blocked on a human decision. Set it when you escalate; the CTO/human clears it. Do not merge or advance an issue while it is set.

Every comment, PR description, and PR review you write as agent `NAME` begins with the line `[agent:NAME]`. Comments that carry an action end with a structured trailer:

```
---
agent: NAME
action: handoff_to | status_change | escalation | observation | review_request | complete
to: target-agent-name (for handoff_to)
```

Operations:

<!-- #665 stage 4: dropped old L263 ("discipline enforced by skill rules through R6; from R7 a Capability Registry entry / agent-MCP server") — it described the discarded R4–R8 roadmap and contradicted "Old R4–R8 roadmap discarded"; flagged on the CTO contradictions track. -->
- **handoff_to / review_request** — reassign the issue to the next owner or reviewer (`code-reviewer`, `be-test-reviewer`, or an architect via the coordinator) with explicit acceptance criteria; post the matching trailer.
- **escalate_to_human** — (1) reassign to the CTO/Reza, (2) set `needs-human`, (3) post an escalation comment with a specific question. All three together; partial escalations are bugs.
- **complete** — a run may only end by reassigning to a named next owner, creating an assigned child issue, or escalating with a specific question — never "done, nothing assigned". A brief is not done until at least one child implementation issue exists. Post a completion comment summarising delivery against acceptance criteria.
- **observe** — a comment with the `observation` trailer; no reassignment.

## 6. Repository layout

`services/<service>/` directories are vertical: each owns its code, tests, Dockerfile, and operator-facing README. `packages/` is shared infrastructure: adding a package requires `solution-architect` approval (via the coordinator); cross-service coupling goes through `packages/` or service APIs. Deviations from this shape require an ADR. The legacy `oraclous-core-service` directory is deleted — never recreate it; never commit `__pycache__`/`*.pyc`. Read the Migration Plan (`docs/knowledge-links.md`) before touching a service to understand which migration phase you are in.

## 8. Gates

Review depth follows severity. High severity — all backend application code, plus infra touching auth/data/billing/secrets/IAM — gets the full gate below. Low severity — infra not touching those surfaces, and docs — gets a light gate: at least one non-implementer reviewer before merge. When in doubt, treat as High. **No agent self-merges; the PR author is never the sole merger.**

The full gate for application code:

| From | To | Owner | What's verified |
| --- | --- | --- | --- |
| Backlog | Ready | `product-planner` + `solution-architect` + `security-architect` (coordinator session) | Brief is testable; architecture references present; threat tags set; lift-tag assigned |
| Ready | Tests Authoring | `test-author` | Pickup |
| Tests Authoring | Tests Review | `test-author` | `[tests]` PR opened with failing tests; legacy tests lifted first for Lift/Reshape/Extract |
| Tests Review | Implementation | `be-test-reviewer` | Tests assert the right boundary; security tests genuinely exercise threats; merge `[tests]` PR. Decision-level problems escalate to coordinator architects |
| Implementation | Code Review | `backend-implementer` | `[impl]` PR with green tests |
| Code Review | CTO sign-off | `code-reviewer` + `qa-engineer` + `security-architect` if security-touching | Craft, coverage, security, architecture all signed off |
| CTO sign-off | Done | CTO agent | Final sign-off; CTO merges the `[impl]` PR and records it in the merge digest for Reza's async spot-audit |

The Backlog → Ready gate happens entirely in the coordinator session before an issue reaches this repo; `[impl-infra]` and `[docs]` PRs are opened by `devops-implementer`/`docs-writer` from the coordinator session, not here. Reza merges only at release level. Definition of Done page: `docs/knowledge-links.md`.

## 9. Done means done

> **⚠️ DEPLOYED-STACK VERIFICATION LAW (non-negotiable, do not bypass).** Not tested, not done, until driven against the DEPLOYED docker stack (`deploy/docker-compose.yml` [+ `docker-compose.dev-ports.yml`]) through the application-gateway on `:8006`, via its real HTTP API or a real MCP server. Full text and rationale: `FUCK_CLAUDE_FUCK_PAPERCLIP.md` rules 1 and 5.
>
> - Gateway only — never a service port, never `/internal`; a real JWT from a real registration.
> - The user brings their own data, model and token through the public APIs — never injected server-side, never hardcoded in the test.
> - Forbidden as a substitute for the deployed proof: fakes/monkeypatch, internal-function calls, DB-direct assertions.
> - CI-green, unit and testcontainers are necessary, never sufficient — they run a *hypothesised* stack and never exercise the engine↔worker↔harness wiring, the broker, or the registry seed.
> - Acceptance bar: rebuild the changed images from current `main`, recreate the services, wait healthy, prove the bound behaviour with `curl` against the live endpoints.

A story is done when, and only when:

1. **CI is green** — lint (ruff + mypy + import contracts + guardrails), unit, integration (via testcontainers/docker), and security-if-applicable all pass.
1b. **Deployed-stack e2e proven** — the bound behaviour is demonstrated against the deployed docker stack via its real HTTP API, through the application-gateway (the law above), not testcontainers/mocks/DB-direct alone. CI-green alone never satisfies this.
1c. **E2E run locally before the PR is opened, PASS pasted into the PR** (`scripts/e2e.sh --up`) — CI's `e2e` job also builds and drives the real stack through the gateway, but keyless it runs the fake harness (`HARNESS_LLM_MODE=fake`; the BYOM real-LLM leg needs the `OPENROUTER_API_KEY` secret), and a fake-LLM run is never a DoD proof (rule 8) — so the local pre-PR run stands; the suite auto-skips when the gateway is down and a skip is not a pass (rule 3).
2. The `[tests]` PR and the `[impl]` PR are both merged — "PR opened" is not done.
3. **Reviewed by a non-implementer** (full or light gate per §8); every required reviewer signed off explicitly (no silent approvals); the PR author was never the sole merger.
4. The CTO merged the PR and recorded it in the merge digest. For a behaviour-touching PR the CTO verifies the real gateway/MCP e2e PASS on the deployed stack before merging — never on CI-green alone (rule 4).
5. Coverage on new code is adequate; no new flaky tests; no regressions in the full suite (a regression in a *different* story → §10; it never holds the current story hostage).
6. If service behaviour changed: `docs-writer` has updated the affected service reference page or has an open assigned issue to do so.
7. If architecture-significant: a follow-up ADR issue is open (ADRs are accepted by the CTO).
8. The issue is closed per the run-completion contract (§5) — never left "done, nothing assigned". Human-approval issues stay open until Reza explicitly approves.

## 10. CI responsibility

- The implementer fixes their own test/lint/type/format failures (including local pre-push gate failures, §4.7) — a PR is not done until green.
- A failure that is actually a regression in a different story → file a separate critical `[regression]` issue (linked, assigned); it does not hold the current story hostage.
- **Security-marked test** failures → `security-architect` (via the coordinator). Overall red-PR board health → the CTO (daily board-check). CI workflow files (`.github/workflows/*`) → `devops-implementer` (via the coordinator); never edit them from an application-code PR.
- **Type gate (WP-7, A6 — ratchet COMPLETE, #366).** CI's `lint` job and the pre-push hook run `uv run mypy services packages` error-free; there is no lenient set. New code lands typed; never reintroduce an `ignore_errors` override; no bare `# type: ignore` (always a `[error-code]`).

The implementer rebases their own branch when its base moves or CI goes red from drift — without waiting or asking. Stacked PRs rebase onto the new base and re-run CI before CTO review/merge. Only genuinely unresolvable conflicts escalate to the CTO.

## 11. What never to do

These are rejected at review with no negotiation:

- Add a code path that reads or writes without `organisation_id`.
- Connect to Postgres as a superuser or `BYPASSRLS` role, or bind the org-GUC at session scope on a pooled connection — both silently void the RLS backstop (ADR-012).
- Bypass the Substrate's ReBAC for a cross-organisation operation.
- Add an upward import (Substrate importing from Capability Registry, etc.).
- Modify tests during implementation to make them pass.
- Use `latest` for a Docker base image or any dependency version.
- Add a credential path that lets Oraclous-the-company staff decrypt customer data in cloud-hosted mode.
- Invoke a capability without writing provenance.
- Merge a PR without explicit non-implementer reviewer sign-off, while its needs-human flag is set, or as the PR author (no self-merge — the CTO merges).
- `git push` without first running the mandatory local pre-push gate (§4.7).
- Bundle unrelated changes into one commit, or add a forbidden attribution trailer to a commit/PR (§4.5).
- Leave uncommitted changes in the shared checkout, or run two concurrent write-tasks against this repo (§4.8).
- Reproduce verbatim text from a customer's manifest, prompt, or output in error messages, logs, or test fixtures.
- Add or modify ADRs directly — propose to `solution-architect` (the CTO accepts them).
- Edit knowledge-base architecture pages directly — propose to `solution-architect`. (Confluence is a read-only mirror; do not edit it at all.)
- Treat a flaky test as "noise" — flakiness is a bug.
- Hand-roll a fetch call from a service when the typed client could be used.
- Write platform code that *is* the harness (rather than interpreting harnesses).
- Read or write the `legacy-reference/` directory's git state — it is a read-only worktree.
- Default to a greenfield rewrite when the story carries a `Lift`, `Reshape`, or `Extract` tag — honour the tag and start from the named legacy source (§12).
- Define a cross-repo data shape, API response, or relation locally — open a `Contract` issue and stop (§12).

## 12. Legacy reference and cross-repo shapes

The previous backend codebase is available read-only at `/Users/reza/workspace/OraclousAI/legacy-reference/old-backend/` — a git worktree pinned to the `develop` branch (the most current branch of that codebase). Never write to it; if it appears to be on a branch other than `develop`, that is a setup error — surface it to the human and stop, do not switch branches yourself. It is reference material for behaviour to preserve, read in light of the story's lift-tag; when in doubt, the canonical knowledge base wins, this `CLAUDE.md` wins, the legacy code is the behavioural reference. For a `Greenfield`-tagged story, do not copy legacy directory structure, naming, or service boundaries unless they explicitly match the architecture. For the lift-vs-rewrite rubric and honouring a `Lift`/`Reshape`/`Extract`/`Greenfield` tag, use the `legacy-lift-and-reshape` skill.

**Cross-repo shapes are not yours to define.** A data shape, API response, or relation that crosses the repo boundary (anything the frontend also consumes, any contract between two services) is never defined locally: open a `Contract` issue on GitHub, assign it to `solution-architect`, then stop (Cross-cutting agreement protocol: `docs/knowledge-links.md`). The shape is decided and recorded canonically in `oraclous-knowledge` before either side implements. Defining a cross-repo shape locally is a process violation of the same class as editing tests to make them pass.

<!-- #665 stage 4: "where Contracts originate" (design tier, OAuth-bridge example) compressed to one line; the rule (Contract issue → solution-architect → recorded → implement) unchanged. -->
A Contract may originate from the frontend `experience-architect` (design tier) framing a user-facing gateway gap; `solution-architect` still owns the system shape — treat it like any other Contract.

## 14. Working with this file

This file is owned by `docs-writer`. Material changes go through a `[docs]` PR with `docs-writer` as the author, a non-implementer reviewer, and CTO merge; cosmetic fixes batch into a periodic `[chore]` PR. Found a gap — something an agent needed and couldn't find? Open a `docs-writer` issue; do not silently add it. This file is short on purpose. Lost prior session context mid-task? Use the `resume-after-context-reset` skill.
