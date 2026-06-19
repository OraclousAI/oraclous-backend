# CLAUDE.md — oraclous-backend

This file is the working contract for any AI agent (Claude Code, an agent in the harness runtime, or otherwise) operating in this repository. Read it in full at the start of every session.

This repo is **`OraclousAI/oraclous-backend`** — the Python codebase for the Oraclous Platform. It is a working **8-service platform** built end-to-end through R7-SEC, each service under `services/<service>/` and layered per ORAA-4 §21 (`routes → services → domain → repositories → core`): `auth-service` (identity, orgs, roles), `credential-broker-service` (encrypted connections + per-org KMS envelope), `knowledge-graph-service` (ingest → graph), `knowledge-retriever-service` (search + subgraph), `capability-registry-service` (tools/connectors + MCP import), `harness-runtime-service` (R4 OHM agent runtime), `execution-engine-service` (R5 durable orchestration), and `application-gateway-service` (R6 edge — the sole external surface).

**Operating model (current):** work is tracked as **GitHub Issues + PRs in this repo**, driven via the **`gh`** CLI; agents pick up issues by assignee/label. The ORAA-4 gates, the `.githooks` (pre-push + commit-msg), and the `main` branch ruleset below are enforced and current. The governance **rules** (gates, no-attribution, one-commit-per-concern, non-author review, up-to-date base) apply throughout; the **board** is GitHub Issues.

---

## 0. Operating Contract (single authority)

All agents operating in this session are governed by the **ORAA-4 Operating Contract** (`operating-contract`) — the canonical source for gate→owner maps, run-completion rules, review depth, workspace discipline, and engineering governance.

**When this file and ORAA-4 diverge, ORAA-4 wins.** Open a `docs-writer` issue to reconcile this file.

Key provisions every agent must observe:

- **§5 Pre-push gate is an enforced hook.** This repo ships `.githooks/pre-push` (`core.hooksPath=.githooks`); a push that fails is **blocked locally**. The hook mirrors the **full CI `quality` job** (ruff check/format, mypy, import-contracts, org-scoping, labels-schema, test-import hygiene, neo4j write-role, contract checksums) — not a subset (see §4.7).
- **§6 Review depth + server-side gate.** High-severity changes get the full gate; low-severity get a light ≥1-reviewer gate; when in doubt, treat as High (see §8). `main` is protected by a **GitHub ruleset** (public repo, no admin bypass): required CI checks + a non-author approving review + up-to-date base. The CTO merges via `oraclous-knowledge/operations/gated_merge.sh`. See ORAA-4 §20.
- **§12 Workspace discipline.** Per-run git worktrees are currently OFF; every writer shares one checkout, so writer runs serialize and always end clean (see §4.8).
- **Run-completion.** A run may only end by reassigning the issue to a named next owner, creating an assigned child issue, or escalating with a specific question — never "done, nothing assigned" (see §5.4). A brief is not done until at least one child implementation issue exists.

---

## Governance gates — canonical in ORAA-4

This is a pointer, not a restatement: ORAA-4 (`operating-contract`) is authoritative, and on any divergence ORAA-4 wins. The gates that bite most in this repo:

- **§5 commits + pre-push + no attribution.** Commit messages are `[ORAA-xx] [agent:NAME] msg`, one commit per concern. Never write `Co-Authored-By`, `Generated`, `claude`, or 🤖 in commits, PR bodies, or comments. The `pre-push` hook (mirroring the full CI `quality` job) and the `commit-msg` hook (commit format + no-attribution) are both wired via `core.hooksPath=.githooks` and block bad pushes/commits locally.
- **§5 PR-BUNDLING LAW (non-negotiable).** **Never ship a one-commit-per-PR stream.** "One commit per concern" means **multiple commits inside ONE PR**, NOT one PR per commit. Bundle related concerns into a single PR — CI (~6 min) + non-author review + redeploy run **once per PR**, so a separate PR per commit multiplies the cost. An issue with N sub-tasks ships as **one PR with N commits, never N PRs** (e.g. a mypy + OTel + Celery issue = one PR / three commits). Default to **fewer, bigger PRs**; the only exception is changes in different repos (which can't share a PR).
- **§13.1 pre-open readiness.** Before OPENING a PR for review it must be pre-push-clean, CI-green, and rebased onto current `main` (not BEHIND). You own this; a reviewer never discovers red CI or a needed rebase.
- **§13.4 branch-from-merged-tests.** An `[impl]` PR branches from / rebases onto the commit where its `[tests]` PR merged, before opening — this kills add/add conflicts and preserves ADR-010 two-PR independence.
- **§9 DoD + handoff.** Done = CI-green + mergeable + non-implementer review + PR merged + handed off to the next owner (§9.1 — never finish your part and leave the issue parked). Small conflicts/misalignments are folded into the current PR, not new tickets (§9.2).
- **§9.3 docker.** Multi-service functionality is `docker-required`; run its integration tests on Docker. If the daemon is down, raise an error and block `needs-human` — never skip.
- **§17 structure.** New code lives under `services/<service>/`; do not extend the legacy `oraclous-core-service`; never commit `__pycache__`/`*.pyc`.
- **§21 canonical service architecture (R3.5).** Every service follows the layered structure `routes → services → domain → repositories → core` (package root `src/oraclous_<svc>_service/`). **No business logic, no DB drivers, and no non-`BaseModel` class defs in `routes/`; repositories are the ONLY DB/Neo4j/Redis access.** Enforced by `tools/lint/check_service_structure.py` + `check_no_stubs.py` + per-service `[tool.importlinter]` contracts (CI `lint` + pre-push). Standard: `oraclous-knowledge/engineering/service-architecture-standard.md` (ORAA-4 §21).
- **§22 hardened per-service DoD (R3.5).** A SERVICE is done only by 8 gates: structure + **not-hollow** (`check_no_stubs` zero findings; flip `tools/lint/service_status.yaml`) + runs (`docker compose up` healthy) + real endpoints (integration vs real substrate) + **smoke vs real substrate** (`smoke.sh`, the `r3_5_gate` CI job) + **Reza sign-off** (`needs-human`). A stub never passes done.
- **§23 R3.5 delivery.** Active release: rebuild every service real, **per service**, in **≤6 coarse vertical slices** (no micro-tickets). Spec = legacy `develop@84152635` (`git show develop:<path>`; never write `legacy-reference`). `oraclous-core-service` = salvage-then-delete (human-gated). Old R4–R8 roadmap discarded.
- **§16 KB currency.** If you change `oraclous-knowledge`, keep the docs current and refresh graphify in the same change.
- Full text: ORAA-4 + `oraclous-knowledge/engineering/`.

---

## 1. Identity and scope

This is the **backend execution** repository. The personas that live and act in this repo session are:

| Agent | Activity here |
| --- | --- |
| `backend-implementer` | Authors all production Python code (`[impl]` PRs) |
| `test-author` | Authors tests *before* implementation (`[tests]` PRs) |
| `be-test-reviewer` | Reviews `[tests]` PRs at the Tests Review gate (the narrow BE-only architecture+security verification persona) |
| `code-reviewer` | Always on every `[impl]` PR for craft review |
| `qa-engineer` | Verifies test suite, coverage, flakiness; authors regression tests under `tests/` |

The **CTO agent** holds full technical authority over this repo: it signs off final gates, merges feature PRs, accepts ADRs, and approves architecture/release changes. It escalates to the human (Reza Jahankohan) only when something is ambiguous, blocked, or out-of-policy. See **§8 Gates** below.

### Personas that do NOT live here

Planning, architecture, cross-cutting agreement, infra, and documentation happen in the **coordinator** session at the workspace root — not here. Specifically:

- `product-planner`, `solution-architect`, `security-architect`, `devops-implementer`, `docs-writer` all live in the coordinator session. You receive **ready, briefed issues** with lift-tags from them via GitHub issue assignment; you do not plan or architect here.
- When this session needs an architecture decision, a Contract, a brief fix, threat tagging, infra, or a doc change, it **escalates to the coordinator** by reassigning the issue to the relevant coordinator persona — it does not load that persona here.
- The one apparent exception is review at the Tests Review gate: that is `be-test-reviewer` (a distinct narrow persona that lives here), **not** `solution-architect`/`security-architect`. `be-test-reviewer` verifies tests against already-made decisions and escalates decision-level problems up to the coordinator.

Canonical residency map: [Session topology and persona residency](https://oraclous.atlassian.net/wiki/spaces/OP/pages/1736705) *(read-only Confluence mirror)*. Full skill definitions: [Agent Skills Catalogue](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753852) *(read-only mirror)*. Read your own skill page on session start.

---

## 2. Source of truth

**The `oraclous-knowledge` git repository is canonical.** It is the single source of truth for architecture, ADRs, governance, and engineering process. **Confluence is now a read-only mirror** of that knowledge base — consult it for convenience, but when it disagrees with `oraclous-knowledge` or with shipped reality, the knowledge repo wins. When this file disagrees with the canonical knowledge base, the knowledge base wins; open a `docs-writer` issue to reconcile this file.

This file summarises the backend invariants and points at the knowledge base for everything that evolves. The pages an agent in this repo consults most often (linked here to their read-only Confluence mirror):

| Need | Page (read-only mirror) |
| --- | --- |
| Architecture overview | [Platform Architecture v1.1](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753707) |
| Layer model | [Section 3 — Layered Architecture](https://oraclous.atlassian.net/wiki/spaces/OP/pages/65967) |
| Manifest format (narrative) | [Section 4 — Manifest Format Specification](https://oraclous.atlassian.net/wiki/spaces/OP/pages/425993) |
| Manifest format (spec) | [OHM v1.0 Standalone Specification](https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501) |
| Flows | [Section 5 — Flows](https://oraclous.atlassian.net/wiki/spaces/OP/pages/426016) |
| Governance | [Section 6 — Governance Model](https://oraclous.atlassian.net/wiki/spaces/OP/pages/720900) + [Structured Governance Taxonomy](https://oraclous.atlassian.net/wiki/spaces/OP/pages/688439) |
| Security threats | [Section 6.5 — Security Threats and Mitigations](https://oraclous.atlassian.net/wiki/spaces/OP/pages/851990) + [Structured Threat Catalogue](https://oraclous.atlassian.net/wiki/spaces/OP/pages/983129) |
| Portability | [Section 7 — Portability Story](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753728) |
| Migration plan | [Section 8 — Consolidation and Migration Plan](https://oraclous.atlassian.net/wiki/spaces/OP/pages/688329) |
| Releases (current + planned) | [09. Releases](https://oraclous.atlassian.net/wiki/spaces/OP/pages/164160) |
| ADRs | [02. ADRs](https://oraclous.atlassian.net/wiki/spaces/OP/pages/589826) |
| Per-service reference | See **§7 Services** below |
| Test strategy | [Test Strategy](https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940) |
| Code style | [Code Style Guide](https://oraclous.atlassian.net/wiki/spaces/OP/pages/426037) |
| Git workflow | [Git Workflow](https://oraclous.atlassian.net/wiki/spaces/OP/pages/131103) |
| PR conventions | [PR Conventions](https://oraclous.atlassian.net/wiki/spaces/OP/pages/393465) |
| Definition of Done | [Definition of Done](https://oraclous.atlassian.net/wiki/spaces/OP/pages/66010) |

The master board for all work is **GitHub Issues + PRs**, not any of the above. Work is organised as Goals (releases) → Projects (epics) → Issues; agents pick up issues by assignee/label, driven via the `gh` CLI. Your work is whatever is assigned to you on GitHub (see §5).

---

## 3. Architecture invariants

These are non-negotiable. A PR that violates any of them is rejected at review regardless of how well the tests pass.

### 3.1 The four layers

The platform has exactly four layers. Code lives in one of them. Imports go downward only.

```
Layer 4: Application Gateway      → application-gateway-service
Layer 3: Harness Runtime + Engine → harness-runtime-service, execution-engine-service
Layer 2: Capability Registry      → capability-registry-service
Layer 1: Substrate                → auth-service, credential-broker-service,
                                    knowledge-graph-service, knowledge-retriever-service
```

- Substrate never imports from the layers above it.
- Capability Registry imports only from Substrate.
- Harness Runtime imports from Substrate and Capability Registry.
- Application Gateway may import from anything below.
- No service has its own database access bypassing the Substrate primitives.

Reference: [ADR-001 — Four-Layer Architecture](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753752).

### 3.2 OHM is the canonical manifest format

Every harness, every capability descriptor, every policy set is OHM. Code that produces or consumes harness configuration speaks OHM, not a service-local format. The platform converts to/from external formats (Claude Code skills, LangGraph, Codex agents) at adapter boundaries only.

Reference: [ADR-002 — OHM as Canonical Manifest Format](https://oraclous.atlassian.net/wiki/spaces/OP/pages/557058) and the OHM v1.0 Spec.

### 3.3 organisation_id is on every storage operation

Every write to the Substrate carries `organisation_id`. Every read is parameterised by `organisation_id`. There is no code path that reads or writes without it. This is the foundation of per-organisation isolation.

Tenant-scoped substrate access goes through the `oraclous_substrate.access` seam (the `scoped_*` functions), which sources `organisation_id` from the authenticated org-context and fails closed when none is bound. **App-layer org-scoping (every read/write parameterised by `organisation_id`) is the primary, live tenancy control.** The Postgres **RLS backstop** described in ADR-012 §2 is **now realized across all 7 services** (epic `oraclous-backend#353` closed, [ADR-030](https://oraclous.atlassian.net/wiki/spaces/OP/) — 2026-06-17): every service connects at runtime as the `NOSUPERUSER`/`NOBYPASSRLS` `oraclous_app` role, with `ENABLE`+`FORCE ROW LEVEL SECURITY` + an org-isolation policy on every org-scoped table (27 forced-RLS tables). The org-GUC (`app.current_organisation_id`) is bound transaction-locally per request by the substrate `install_org_guc_guard`/`org_scope` seam (`oraclous_substrate.access_async`); the dev `oraclous_app` password must be overridden with a managed credential in prod. So RLS is the realized defense-in-depth **second** line — but **app-layer `WHERE organisation_id = …` remains the primary control**: a request-path DB op that runs *without* binding the org (no `org_scope`/`use_organisation_context`) hits an empty GUC and fail-closes (zero rows / 42501) under `oraclous_app` — bind the org on every request-path op (the `check_rls_request_binding` guardrail enforces a service-level presence check; the `check_service_dep_imports` guardrail enforces that a service declares the packages it imports).

Reference: [ADR-006 — Organisation as Outermost Tenancy Unit](https://oraclous.atlassian.net/wiki/spaces/OP/pages/393403), [ADR-012 — Substrate Tenancy Enforcement Seam and RLS Backstop Preconditions](https://oraclous.atlassian.net/wiki/spaces/OP/pages/2490396) (RLS now realized — see its as-built note), and **ADR-030 — Realize the Postgres RLS Backstop** (the realization design; #353 closed).

### 3.4 ReBAC mediates every cross-organisation traversal

If an operation reads or writes data belonging to an organisation other than the actor's home organisation, the operation calls the Substrate's access decision API first. Direct database queries that bypass ReBAC are forbidden.

Reference: [ADR-004 — Federation via ReBAC Traversal](https://oraclous.atlassian.net/wiki/spaces/OP/pages/131083).

### 3.5 Fail-closed defaults

When an authorisation check returns ambiguous, the code denies. When a content hash doesn't match, the code rejects. When a budget check fails, the code halts. There is no "if in doubt, allow" path anywhere.

### 3.6 Operator separation in cloud-hosted mode

In the cloud-hosted deployment, Oraclous-the-company staff cannot decrypt customer BYOM credentials or customer data. Code that would weaken this — for any reason, including "for support" or "for debugging" — is rejected. The KMS envelope is held outside Oraclous's control.

Reference: [ADR-008 — Cloud-Hosted Mode with Equivalent Data Sovereignty](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753792).

### 3.7 Provenance on every capability invocation

Every capability dispatch produces a provenance record. There is no code path that invokes a capability without writing provenance. Provenance writes go through the runtime's single collector, not direct database writes.

Reference: [Section 6 — Governance Model](https://oraclous.atlassian.net/wiki/spaces/OP/pages/720900) and threat catalogue entry T7.

### 3.8 Harnesses are descriptors, not code

A harness is OHM (a manifest). It is not a Python class. The harness runtime *interprets* harnesses; it does not compile them into platform code. The compiler harness (R7) and consciousness skills are themselves harnesses, not platform code.

Reference: [ADR-003 — Platform-as-Code, Actors-as-Harnesses](https://oraclous.atlassian.net/wiki/spaces/OP/pages/884737) and [ADR-005 — Workflow Concept Retirement](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753772).

---

## 4. Working agreement

### 4.1 TDD is the contract

Every issue that touches code follows the test-first flow:

1. `test-author` opens a `[tests]` PR with failing tests against the empty/existing code.
2. The `[tests]` PR is reviewed at the Tests Review gate by `be-test-reviewer` (architectural + security verification against already-made decisions); decision-level problems escalate to the coordinator's `solution-architect`/`security-architect`.
3. The `[tests]` PR merges.
4. `backend-implementer` opens an `[impl]` PR with the minimum code that turns the failing tests green.
5. The `[impl]` PR is reviewed by `code-reviewer` (always), `qa-engineer` (always), and any architects whose surfaces are touched.
6. The CTO agent gives final sign-off and **merges** the `[impl]` PR.

The implementer **never** modifies tests to make them pass. If a test is wrong, that is a discovery: flag it to `test-author` with the specific reason and propose a corrected test.

**Import not-yet-built intra-repo seams function-locally.** A `[tests]` PR lands tests for a seam (`oraclous_*`) before its `[impl]` exists. If those tests import the not-yet-built seam at *module level*, `pytest` aborts collection (exit 2) for the **whole** run — reddening every open PR's quality/integration/security gate until the `[impl]` lands. Instead, import the seam **inside the test or fixture** (function-locally): the module collects cleanly and the test fails at *runtime* with `ModuleNotFoundError` — RED-by-design, on its own marker only, never masking other suites. Never convert a missing intra-repo seam into a *skip* (`pytest.importorskip("oraclous_…")` or `try/except ImportError → pytest.skip`): a skip turns missing coverage green, and for a `security`-marked test that hides an unverified threat behind a green gate. A missing intra-repo seam must hard-fail, never skip. Enforced by the `check_test_imports` guardrail (TST001/TST002) in CI; the rule self-clears once the `[impl]` lands. The mandatory pre-push `pytest --collect-only` (§4.7) catches function-local-import violations before they ever reach CI. (ORAA-48; security-architect coverage-safety concurrence.)

Reference: [ADR-010 — Test-Driven Development with Test-Author Agent](https://oraclous.atlassian.net/wiki/spaces/OP/pages/557078).

### 4.2 PR naming

| Prefix | Meaning | Author |
| --- | --- | --- |
| `[tests]` | Tests-only PR (failing tests, no implementation) | `test-author` |
| `[impl]` | Implementation PR against merged tests | `backend-implementer` |
| `[impl-infra]` | Infrastructure changes (Docker, compose, Helm, workflows) | `devops-implementer` |
| `[regression]` | Regression test for a discovered bug | `qa-engineer` |
| `[docs]` | Repo-level docs (this file, READMEs) | `docs-writer` |
| `[chore]` | Dependency bumps, version pins, formatting passes that don't touch behaviour | any implementer |

### 4.3 PR sizing

Target under 300 net lines of code per PR. If you cross that, justify it in the description. If the change is naturally large, request a split before opening the PR.

### 4.4 Branch model

`main` is protected; no direct pushes. Work happens on branches named `<agent-name>/<issue-key>-<slug>`, e.g. `backend-implementer/ORAA-178-organisation-id-on-substrate-writes`. The issue key is the GitHub issue identifier (e.g. `ORAA-178`).

### 4.5 Commits

Every commit message follows:

```
[ORAA-42] [agent:backend-implementer] Short imperative description

Longer body if needed.
```

The agent prefix is part of the commit message because all agents share the human GitHub account; the prefix is how the audit trail attributes work to agents.

**One commit per concern** — never bundle unrelated changes into a single commit. **Forbidden in any commit message** (and any PR body or review): `Co-Authored-By` in any variant, "Generated with"/"Generated by", `claude.ai`, any Anthropic attribution, and the robot emoji. This is enforced by `.githooks/commit-msg` wired in via `core.hooksPath`.

### 4.6 Spikes are explicit

Prototype or exploratory work that does not follow TDD is a **spike** and must be marked as such on the GitHub issue and in the PR title (`[spike]`). Spikes do not merge to `main`; they produce findings that feed a normal TDD issue.

### 4.7 Mandatory local pre-push gate (ORAA-4 §5)

Before **any** `git push`, run — locally — the same cheap checks CI's `quality` job runs, and push only if they are clean:

```
uv run ruff check . && uv run ruff format --check . && uv run pytest --collect-only
```

`pytest --collect-only` automatically catches function-local-import violations (§4.1) before they redden CI for every open PR. A push that fails these checks is the implementer's own responsibility to fix before re-pushing — it does **not** become a separate `[fix]` issue.

### 4.8 Workspace discipline (ORAA-4 §12)

Per-run git worktrees are currently **OFF**, so every agent that writes this repo shares **one** checkout. Therefore:

- Writer runs operate with `maxConcurrentRuns=1`; the CTO must not route two concurrent write-tasks to the same repo.
- Every writer run **starts clean** — check out the intended base before working.
- Every writer run **ends clean** — commit and push all of its changes; never leave uncommitted changes in the shared checkout.
- Use issue **blocking** to serialize same-repo work so two writers never collide on the shared checkout.

---

## 5. Agent identity and the board (operational)

Agent identity is **GitHub issue assignment** — the agent the issue is assigned to *is* the acting persona. There is no separate identity field; whoever the issue is assigned to owns it.

### 5.1 Your work

Your work is the set of GitHub issues assigned to you. When you pick up an issue, read it and its comments first — the last `[agent:NAME]` comment with an action trailer tells you where the work stands.

### 5.2 The `needs-human` attention flag

GitHub issues carry a **`needs-human` label**. Set it when you escalate to the human; the CTO/human clears it when the escalation is resolved. It is the controlled signal that an issue is blocked on a human decision — do not merge or advance an issue while its `needs-human` label is set.

### 5.3 Comment prefix on everything you write

Every comment, PR description, and PR review you write while acting as agent `NAME` begins with the line:

```
[agent:NAME]
```

Comments that carry an action end with a structured trailer:

```
---
agent: NAME
action: handoff_to | status_change | escalation | observation | review_request | complete
to: target-agent-name (for handoff_to)
```

### 5.4 Operations (GitHub)

| Operation | Implementation |
| --- | --- |
| `my work` | The GitHub issues assigned to you |
| `handoff_to` | Reassign the issue to the next owner with explicit acceptance criteria; post a handoff comment with the `action: handoff_to` trailer |
| `escalate_to_human` | (1) Reassign the issue to the CTO/Reza. (2) Set the issue's `needs-human` label. (3) Post a structured escalation comment with a **specific question** and the `action: escalation` trailer. **All three together; partial escalations are bugs.** |
| `complete` | Per the run-completion contract: a run may only end by **reassigning to a named next owner**, **creating an assigned child issue**, or **escalating with a specific question** — never "done, nothing assigned". Post a completion comment with the `action: complete` trailer summarising delivery against acceptance criteria |
| `observe` | Post a comment with the `action: observation` trailer; no reassignment |
| `review_request` | Reassign the issue to the reviewer per the work (`code-reviewer`, `be-test-reviewer`, or an architect via the coordinator); post the `action: review_request` trailer |

This discipline is enforced by skill rules through R6. From R7 onward it is additionally enforced by a Capability Registry entry — the small standalone agent-MCP server listed as an R7 deliverable.

---

## 6. Repository layout

The repo holds the 8 services above under `services/<service>/`, each layered `routes → services → domain → repositories → core` (ORAA-4 §21); shared packages live under `packages/`. New work conforms to this shape; deviations require an ADR.

```
oraclous-backend/
├── CLAUDE.md                       # this file
├── README.md                       # human-facing onboarding (operator perspective)
├── pyproject.toml                  # monorepo root; uv workspace config
├── uv.lock
├── .python-version
├── .editorconfig
├── .gitignore
├── ruff.toml
├── pytest.ini                      # markers: unit, integration, security,
│                                   # isolation, byom, organization_isolation
├── .githooks/
│   └── commit-msg                  # enforces commit policy (§4.5); wired via core.hooksPath
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                  # quality (ruff + collect), tests, type-check on every PR
│   │   ├── security.yml            # security-marked test gate
│   │   └── release.yml             # image build + push on tag
│   └── CODEOWNERS                  # routes review requests
├── deploy/
│   ├── docker-compose.yml          # local self-hosted stack
│   ├── helm/                       # cloud-hosted production charts
│   └── observability/              # logging, metrics, traces configs
├── packages/                       # shared libraries
│   ├── ohm/                        #   OHM types and validators
│   ├── substrate/                  #   ReBAC client, provenance collector
│   ├── governance/                 #   organisation_id propagation utilities
│   ├── provenance/                 #   telemetry, errors, logging
│   ├── rebac/
│   ├── telemetry/
│   └── errors/
├── services/                       # one directory per service
│   ├── auth-service/               #   Each service has:
│   ├── credential-broker-service/  #     - src/<service_name>/
│   ├── knowledge-graph-service/    #     - Dockerfile
│   ├── knowledge-retriever-service/#     - pyproject.toml
│   ├── capability-registry-service/#     - README.md (operator-facing)
│   ├── harness-runtime-service/    #     - tests/
│   ├── execution-engine-service/
│   └── application-gateway-service/
└── tests/                          # cross-service integration tests
    ├── integration/
    ├── security/
    ├── isolation/
    ├── byom/
    └── organization_isolation/
```

### 6.1 `packages/` is shared infrastructure

Code in `packages/` is consumed by multiple services. Adding a new package requires `solution-architect` approval (via the coordinator).

### 6.2 `services/` is vertical

A service owns its own code, tests, Dockerfile, and operator-facing README. Cross-service coupling goes through `packages/` or service APIs.

---

## 7. Services

Eight backend services from [04. Services Reference](https://oraclous.atlassian.net/wiki/spaces/OP/pages/786433) *(read-only mirror)*. Consult the service's reference page before touching its directory.

| Service | Layer | Reference (read-only mirror) | Target shape in |
| --- | --- | --- | --- |
| `auth-service` | Substrate | [Page 622756](https://oraclous.atlassian.net/wiki/spaces/OP/pages/622756) | R1 |
| `credential-broker-service` | Substrate | [Page 753812](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753812) | R1 |
| `knowledge-graph-service` | Substrate | [Page 753832](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753832) | R3 |
| `knowledge-retriever-service` | Substrate | [Page 622776](https://oraclous.atlassian.net/wiki/spaces/OP/pages/622776) | R3 |
| `capability-registry-service` | Capability Registry | [Page 884757](https://oraclous.atlassian.net/wiki/spaces/OP/pages/884757) | R2 |
| `harness-runtime-service` | Harness Runtime | [Page 688350](https://oraclous.atlassian.net/wiki/spaces/OP/pages/688350) | R4 |
| `execution-engine-service` | Harness Runtime | [Page 884777](https://oraclous.atlassian.net/wiki/spaces/OP/pages/884777) | R5 |
| `application-gateway-service` | Application Gateway | [Page 131124](https://oraclous.atlassian.net/wiki/spaces/OP/pages/131124) | R6 |

Some services exist in legacy form at `/Users/reza/workspace/OraclousAI/legacy-reference/old-backend/` (worktree pinned to `develop`). Read [Section 8 — Consolidation and Migration Plan](https://oraclous.atlassian.net/wiki/spaces/OP/pages/688329) before touching any service to understand which migration phase you are in.

---

## 8. Gates

Review depth follows **ORAA-4 §6 severity**. **High severity** — all backend application code, plus infra touching auth/data/billing/secrets/IAM — gets the full gate below. **Low severity** — infra not touching those surfaces, and docs — gets a light gate: at least one non-implementer reviewer before merge. **When in doubt, treat as High.** No agent self-merges; the PR author is never the sole merger.

The full gate for application code:

| From | To | Owner | What's verified |
| --- | --- | --- | --- |
| Backlog | Ready | `product-planner` + `solution-architect` + `security-architect` — **all in the coordinator session** | Brief is testable; architecture references present; threat tags set; lift-tag assigned |
| Ready | Tests Authoring | `test-author` (this session) | Pickup |
| Tests Authoring | Tests Review | `test-author` (this session) | `[tests]` PR opened with failing tests; legacy tests lifted first for Lift/Reshape/Extract |
| Tests Review | Implementation | `be-test-reviewer` (this session) | Tests assert the right boundary; security tests genuinely exercise threats; merge `[tests]` PR. Decision-level problems escalate to coordinator `solution-architect`/`security-architect` |
| Implementation | Code Review | `backend-implementer` (this session) | `[impl]` PR with green tests |
| Code Review | CTO sign-off | `code-reviewer` + `qa-engineer` (this session) + `security-architect` if security-touching | Craft, coverage, security, architecture all signed off |
| CTO sign-off | Done | **CTO agent** | Final sign-off; **CTO merges** the `[impl]` PR and records it in the merge digest for Reza's async spot-audit |

Reference: [Definition of Done](https://oraclous.atlassian.net/wiki/spaces/OP/pages/66010). Note: the Backlog → Ready gate happens entirely in the coordinator session before the issue ever reaches this repo session. Infra (`[impl-infra]`) and docs (`[docs]`) PRs against this repo are opened by `devops-implementer` and `docs-writer` **from the coordinator session**, not here. Reza merges only at release level.

---

## 9. Done means done

A story is **done** when, and only when (Definition of Done, impl/infra):

1. **CI is green** — quality (ruff check + format-check + collect), unit, integration (via testcontainers/docker), and security-if-applicable all pass.
2. The `[tests]` PR and the `[impl]` PR are both **merged** — "PR opened" is not done.
3. It has been **reviewed by a non-implementer** (full or light gate per §8 severity); every required reviewer signed off explicitly (no silent approvals); the PR author was never the sole merger.
4. The **CTO merged** the PR (Reza merges only at release level) and recorded it in the merge digest.
5. Coverage on new code is adequate; no new flaky tests; no regressions in the full suite. A regression discovered in a *different* story is filed as a separate critical `[regression]` issue (linked and assigned) — it does **not** hold the current story hostage.
6. If service behaviour changed: `docs-writer` has updated the affected service reference page or has an open assigned issue to do so.
7. If architecture-significant: a follow-up ADR issue is open if any architectural decision crystallised (ADRs are accepted by the CTO).
8. The GitHub issue is closed by reassigning to a named next owner / spawning a child issue, never left "done, nothing assigned" (§5.4). Human-approval issues stay open until Reza explicitly approves.

---

## 10. CI responsibility

- The **implementer fixes their own** test/lint/type/format failures — a PR is not done until green.
- A failure that is actually a **regression in a different story** → file a separate `[regression]` issue (critical, linked, assigned). It does not hold the current story hostage.
- **Security-marked test** failures → `security-architect` (via the coordinator).
- Overall **red-PR board health** → the **CTO** owns this in the daily board-check.
- **CI workflow files** (`.github/workflows/*`) → `devops-implementer` (via the coordinator); never edit them from an application-code PR.
- A push that fails the **local pre-push gate** (§4.7) is the implementer's own fix before re-pushing — never a separate `[fix]` issue.
- **Type-check ratchet (WP-7, A6).** CI's `lint` job and the pre-push hook run `uv run mypy services packages`. It is type-**GATED** today for `packages/*` + `auth-service` + `knowledge-retriever-service` (errors fail CI). The other six services are kept lenient via `[[tool.mypy.overrides]] ignore_errors = true` in `pyproject.toml` (with a ratchet TODO there). New code lands typed; to tighten a lenient service, fix its mypy errors then delete its override block — never widen the lenient set. No bare `# type: ignore` (always a `[error-code]`).

### 10.1 Rebasing

The implementer **rebases their own branch** when its base moves or CI goes red from drift — do this without waiting or asking. Stacked PRs rebase onto the new base and re-run CI **before** CTO review/merge. Only genuinely **unresolvable** conflicts escalate to the CTO.

---

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
- Define a cross-repo data shape, API response, or relation locally — open a `Contract` issue and stop (§12.4).

---

## 12. Legacy reference and the lift-vs-rewrite default

The previous Oraclous backend codebase is available **read-only** at:

```
/Users/reza/workspace/OraclousAI/legacy-reference/old-backend/
```

It is a **git worktree pinned to the `develop` branch** of the previous backend repository. `develop` is the most current branch of that codebase.

### 12.1 This is a migration, not a rewrite

Most existing backend services are production-grade and correctly factored (`auth-service`, `credential-broker-service`) or sprawling-but-salvageable (`knowledge-graph-builder`). The default for backend work is **lift-and-reshape against the four-layer model** — populate the new repo from the legacy service, then refactor under TDD to the target layer and conventions. **Greenfield is the exception, not the default**, applying only to genuinely new surfaces (the application gateway, the metering subsystem) that have no clean legacy precursor.

> The legacy codebase is always at minimum the **behavioural specification** — even when its code is not reusable. New code passes when it does what the legacy did, plus the architectural invariants. "Start from scratch" must be justified, not assumed.

### 12.2 The lift-vs-rewrite rubric

You do not decide lift-vs-rewrite yourself per file. The verdict is decided once per deliverable in the release page's **Migration source map** (see [09. Releases](https://oraclous.atlassian.net/wiki/spaces/OP/pages/164160) Section 7) and arrives in your story brief as a **lift-tag**: `Lift`, `Reshape`, `Extract`, or `Greenfield`, with the specific legacy source path named. Your job is to honour the tag:

- **Lift** — start from the named legacy code, light refactor only.
- **Reshape** — start from the named legacy logic, refit it to the target layer boundary and conventions (organisation_id, OHM, ReBAC, fail-closed), keep the logic.
- **Extract** — lift the behaviour out of a larger legacy service into its target service.
- **Greenfield** — no usable legacy precursor; write fresh against the architecture. The legacy may still be the spec of what *not* to do.

If a story brief lacks a lift-tag for code that you believe has a legacy precursor, that is a planning gap — flag it to `product-planner` (via the coordinator) rather than silently choosing greenfield.

### 12.3 Rules for the legacy reference

- Reference material for behaviour to preserve, read in light of the lift-tag.
- When in doubt: the canonical knowledge base wins, this `CLAUDE.md` wins, the legacy code is the behavioural reference.
- For a `Greenfield`-tagged story, do not copy legacy directory structure, naming, or service boundaries unless they explicitly match the architecture.
- Never write to `legacy-reference/`. It is a read-only worktree by convention.
- If the worktree appears to be on a branch other than `develop`, that is a setup error — surface it to the human and stop, do not switch branches yourself.

### 12.4 Cross-repo shapes are not yours to define

If you need a data shape, API response, or relation that crosses the repo boundary (anything the frontend also consumes, anything that is a contract between two services), **you do not define it locally**. You open a `Contract` issue on GitHub and assign it to `solution-architect`, then stop, per the [Cross-cutting agreement protocol](https://oraclous.atlassian.net/wiki/spaces/OP/pages/1245185) *(read-only mirror)*. The shape is decided by `solution-architect` and recorded canonically in `oraclous-knowledge` before either side implements. Defining a cross-repo shape locally is a process violation of the same class as editing tests to make them pass.

**Where some of these Contracts now originate (the design tier).** A cross-repo Contract assigned to `solution-architect` may originate from the frontend **`experience-architect`** (the Design tier): when a user journey needs a gateway capability that does not exist — e.g. an OAuth-connect bridge so a provider token captured at login by `auth-service` becomes resolvable as a tool credential through the broker — `experience-architect` files the gap as a Contract framing the *user-facing requirement*, `solution-architect` owns the system shape, and the paired backend implementing issue lands in this session. Treat it like any other Contract: the shape is decided and recorded in `oraclous-knowledge` before you implement.

---

## 13. Working with the knowledge base

Before reaching into the web or your training, consult the canonical knowledge base (`oraclous-knowledge`). The read-only Confluence mirror lives under space `OP` at `https://oraclous.atlassian.net/wiki/spaces/OP/` — use the URLs in §2 for convenient browsing, but treat anything there as a mirror.

When you discover that a knowledge-base page is stale (shipped reality has moved past what it says), open a `docs-writer` issue; do not edit architecture or ADR pages directly, and never edit the Confluence mirror.

---

## 14. Working with this file

This file is owned by `docs-writer`. Material changes go through a `[docs]` PR with `docs-writer` as the author, a non-implementer reviewer, and CTO merge. Cosmetic fixes can be batched into a periodic `[chore]` PR.

When you find a gap in this file — something an agent needed and couldn't find — open a `docs-writer` issue. Do not silently add it; this file is short on purpose.

---

## 15. Resuming after a context reset

If you are resuming work mid-task and have lost prior session context:

1. Read this file.
2. Read your own skill page from [Agent Skills Catalogue](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753852) *(read-only mirror)*.
3. Read the **ORAA-4 Operating Contract** (`operating-contract`) — the single authority; where it and this file diverge, ORAA-4 wins.
4. Look at GitHub: the issue assigned to you that is in progress is yours.
5. Read that issue's comments; the last `[agent:NAME]` comment with an action trailer tells you where you are.
6. Read the linked tests PR (if at Implementation stage) or the brief (if at Tests Authoring).
7. Before any push, run the mandatory local pre-push gate (§4.7).
8. Continue.

If the trail is broken or contradictory, escalate to the human via the `escalate_to_human` operation in §5.4.
