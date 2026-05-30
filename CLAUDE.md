# CLAUDE.md — oraclous-backend

This file is the working contract for any AI agent (Claude Code, an agent in the harness runtime, or otherwise) operating in this repository. Read it in full at the start of every session.

This repo is **`OraclousAI/oraclous-backend`** — the Python codebase for the Oraclous Platform: substrate, capability registry, harness runtime, execution engine, application gateway, and the supporting services that back them. The repo is currently empty by design; the scaffolding work in R0.5 produces its initial shape.

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

`tech-lead` (the human, Reza Jahankohan) is the final sign-off on every gate that requires human approval. See **§8 Gates** below.

### Personas that do NOT live here

Planning, architecture, cross-cutting agreement, infra, and documentation happen in the **coordinator** session at the workspace root — not here. Specifically:

- `product-planner`, `solution-architect`, `security-architect`, `devops-implementer`, `docs-writer` all live in the coordinator session. You receive **ready, briefed stories** with lift-tags from them via the `Agent Owner` field; you do not plan or architect here.
- When this session needs an architecture decision, a Contract, a brief fix, threat tagging, infra, or a doc change, it **escalates to the coordinator** by setting `Agent Owner` to the relevant coordinator persona — it does not load that persona here.
- The one apparent exception is review at the Tests Review gate: that is `be-test-reviewer` (a distinct narrow persona that lives here), **not** `solution-architect`/`security-architect`. `be-test-reviewer` verifies tests against already-made decisions and escalates decision-level problems up to the coordinator.

Canonical residency map: [Session topology and persona residency](https://oraclous.atlassian.net/wiki/spaces/OP/pages/1736705). Full skill definitions: [Agent Skills Catalogue](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753852). Read your own skill page on session start.

---

## 2. Source of truth

**Confluence is canonical.** This file summarises invariants and points at Confluence for everything that evolves. When this file disagrees with Confluence, Confluence wins; open a `docs-writer` ticket to reconcile this file.

The pages an agent in this repo consults most often:

| Need | Page |
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
| **Agent Identity Convention (canonical)** | [09. Releases](https://oraclous.atlassian.net/wiki/spaces/OP/pages/164160) **Section 6** — authoritative for `Agent Owner` and `needs-human` field handling |
| ADRs | [02. ADRs](https://oraclous.atlassian.net/wiki/spaces/OP/pages/589826) |
| Per-service reference | See **§7 Services** below |
| Test strategy | [Test Strategy](https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940) |
| Code style | [Code Style Guide](https://oraclous.atlassian.net/wiki/spaces/OP/pages/426037) |
| Git workflow | [Git Workflow](https://oraclous.atlassian.net/wiki/spaces/OP/pages/131103) |
| PR conventions | [PR Conventions](https://oraclous.atlassian.net/wiki/spaces/OP/pages/393465) |
| Definition of Done | [Definition of Done](https://oraclous.atlassian.net/wiki/spaces/OP/pages/66010) |

Atlassian cloudId: `1eb21297-5f52-49a0-a303-3436694b148c`. Space key: `OP`. Jira project: `ORA`.

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

Tenant-scoped substrate access goes through the `oraclous_substrate.access` seam (the `scoped_*` functions), which sources `organisation_id` from the authenticated org-context and fails closed when none is bound. Two preconditions keep the row-level-security backstop real, not theatre: the production Postgres role is `NOSUPERUSER`/`NOBYPASSRLS` (a superuser or `BYPASSRLS` role silently bypasses RLS), and the org-GUC (`app.current_organisation_id`) is transaction-local (`SET LOCAL`) or reset before a pooled connection is reused (a stale GUC leaks one organisation's scope to the next caller).

Reference: [ADR-006 — Organisation as Outermost Tenancy Unit](https://oraclous.atlassian.net/wiki/spaces/OP/pages/393403) and [ADR-012 — Substrate Tenancy Enforcement Seam and RLS Backstop Preconditions](https://oraclous.atlassian.net/wiki/spaces/OP/pages/2490396).

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

Every story that touches code follows the test-first flow:

1. `test-author` opens a `[tests]` PR with failing tests against the empty/existing code.
2. The `[tests]` PR is reviewed by `solution-architect` (architectural alignment) and `security-architect` (if security-marked); `code-reviewer` reviews the test code itself.
3. The `[tests]` PR merges.
4. `backend-implementer` opens an `[impl]` PR with the minimum code that turns the failing tests green.
5. The `[impl]` PR is reviewed by `code-reviewer` (always), `qa-engineer` (always), and any architects whose surfaces are touched.
6. `tech-lead` final-approves and the `[impl]` PR merges.

The implementer **never** modifies tests to make them pass. If a test is wrong, that is a discovery: flag it to `test-author` with the specific reason and propose a corrected test.

**Import not-yet-built intra-repo seams function-locally.** A `[tests]` PR lands tests for a seam (`oraclous_*`) before its `[impl]` exists. If those tests import the not-yet-built seam at *module level*, `pytest` aborts collection (exit 2) for the **whole** run — reddening every open PR's quality/integration/security gate until the `[impl]` lands. Instead, import the seam **inside the test or fixture** (function-locally): the module collects cleanly and the test fails at *runtime* with `ModuleNotFoundError` — RED-by-design, on its own marker only, never masking other suites. Never convert a missing intra-repo seam into a *skip* (`pytest.importorskip("oraclous_…")` or `try/except ImportError → pytest.skip`): a skip turns missing coverage green, and for a `security`-marked test that hides an unverified threat behind a green gate. A missing intra-repo seam must hard-fail, never skip. Enforced by the `check_test_imports` guardrail (TST001/TST002) in CI; the rule self-clears once the `[impl]` lands. (ORA-48; security-architect coverage-safety concurrence.)

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

`main` is protected; no direct pushes. Work happens on branches named `<agent-name>/<story-key>-<slug>`, e.g. `backend-implementer/ora-42-organisation-id-on-substrate-writes`. The story key is the Jira issue key.

### 4.5 Commits

Every commit message follows:

```
[ORA-42] [agent:backend-implementer] Short imperative description

Longer body if needed.
```

The agent prefix is part of the commit message because all agents share the human Atlassian/GitHub account; the prefix is how the audit trail attributes work to agents.

### 4.6 Spikes are explicit

Prototype or exploratory work that does not follow TDD is a **spike** and must be marked as such in the Jira ticket and the PR title (`[spike]`). Spikes do not merge to `main`; they produce findings that feed a normal TDD story.

---

## 5. Agent identity convention (operational)

The canonical convention lives in [09. Releases Section 6](https://oraclous.atlassian.net/wiki/spaces/OP/pages/164160). The operational summary for this repo:

### 5.1 The `Agent Owner` Jira custom field

- Field name: `Agent Owner`
- Field ID: `customfield_10074`
- Type: single-select
- Values: the 11 agent persona names plus `human` (option ID `10031`)
- Set this field to your persona's name while you are acting on a ticket. Update it when handing off.

### 5.2 The `needs-human` attention flag

- Field name: `needs-human` (display label may vary)
- Field ID: `customfield_10075`
- Type: **multi-checkbox custom field** (NOT a Jira label)
- Option value: `needs-human`, option ID `10032`
- To flag a ticket: write `customfield_10075: [{id: "10032"}]` via the Atlassian MCP.
- To clear: write `customfield_10075: []`.
- Query for tickets needing human attention: `project = ORA AND cf[10075] = "needs-human"`.

> **Why a multi-checkbox and not a label?** It is controlled (you can't typo it), it can't be removed by someone unfamiliar with the convention, and it is more queryable. This is the deliberate design.

### 5.3 Comment prefix on everything you write

Every Jira comment, every Jira worklog, every Confluence inline comment, every GitHub commit message, every GitHub PR description, and every GitHub PR review comment you write while acting as agent `NAME` begins with the line:

```
[agent:NAME]
```

Comments that carry an action end with a structured trailer:

```
---
agent: NAME
action: handoff_to | status_change | escalation | observation | review_request | complete
to: target-agent-name (for handoff_to)
from_status: STATUS (for status_change)
to_status: STATUS (for status_change)
```

### 5.4 Operations

| Operation | Implementation |
| --- | --- |
| `my_tasks` | JQL: `project = ORA AND "Agent Owner" = "<your-name>" AND status != Done ORDER BY priority DESC` |
| `claim_next` | Find highest-priority unassigned ticket where the role matches; set `Agent Owner = $self`; transition to In Progress; post a claim comment |
| `handoff_to` | Set `Agent Owner` to target; transition status; post handoff comment with `action: handoff_to` trailer |
| `escalate_to_human` | (1) Set `Agent Owner = human`. (2) Set `customfield_10075: [{id: "10032"}]`. (3) Post structured escalation comment with `action: escalation` trailer. **All three together; partial escalations are bugs.** |
| `complete` | Transition to Done; post completion comment with `action: complete` trailer summarising delivery against acceptance criteria |
| `observe` | Post comment with `action: observation` trailer; no field or status change |
| `review_request` | Set `Agent Owner` to the reviewer (`code-reviewer`, `security-architect`, or `solution-architect` per the work); transition to In Review; post `action: review_request` trailer |

This is enforced by skill discipline through R6. From R7 onward it is enforced by a Capability Registry entry — the small standalone agent-MCP server listed as an R7 deliverable.

---

## 6. Repository layout

The repo is currently empty. The R0.5 scaffolding work establishes this shape. New work conforms to it; deviations require an ADR.

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
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                  # tests, lint, type-check on every PR
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

Code in `packages/` is consumed by multiple services. Adding a new package requires `solution-architect` approval.

### 6.2 `services/` is vertical

A service owns its own code, tests, Dockerfile, and operator-facing README. Cross-service coupling goes through `packages/` or service APIs.

---

## 7. Services

Eight backend services from [04. Services Reference](https://oraclous.atlassian.net/wiki/spaces/OP/pages/786433). Consult the service's Confluence page before touching its directory.

| Service | Layer | Confluence | Target shape in |
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

| From | To | Owner | What's verified |
| --- | --- | --- | --- |
| Backlog | Ready | `product-planner` + `solution-architect` + `security-architect` — **all in the coordinator session** | Brief is testable; architecture references present; threat tags set; lift-tag assigned |
| Ready | Tests Authoring | `test-author` (this session) | Pickup |
| Tests Authoring | Tests Review | `test-author` (this session) | `[tests]` PR opened with failing tests; legacy tests lifted first for Lift/Reshape/Extract |
| Tests Review | Implementation | `be-test-reviewer` (this session) | Tests assert the right boundary; security tests genuinely exercise threats; merge `[tests]` PR. Decision-level problems escalate to coordinator `solution-architect`/`security-architect` |
| Implementation | Code Review | `backend-implementer` (this session) | `[impl]` PR with green tests |
| Code Review | Done | `code-reviewer` + `qa-engineer` (this session) + `tech-lead` (human, final) | Craft, coverage, security, architecture all signed off; merge `[impl]` PR |

Reference: [Definition of Done](https://oraclous.atlassian.net/wiki/spaces/OP/pages/66010). Note: the Backlog → Ready gate happens entirely in the coordinator session before the ticket ever reaches this repo session. Infra (`[impl-infra]`) and docs (`[docs]`) PRs against this repo are opened by `devops-implementer` and `docs-writer` **from the coordinator session**, not here.

---

## 9. Done means done

A story is **done** when, and only when:

1. Tests PR merged; implementation PR merged; both passed full CI.
2. All gates have been transitioned through in order; no skips.
3. Every required reviewer signed off explicitly (no silent approvals).
4. `Agent Owner` (`customfield_10074`) is set to whoever last touched it (typically `tech-lead` after merge).
5. Coverage on new code is adequate; no new flaky tests; no regressions in the full suite.
6. If service behaviour changed: `docs-writer` has updated the affected service reference page or has an open ticket to do so within the sprint.
7. If architecture-significant: a follow-up ADR is open if any architectural decision crystallised.
8. The Jira ticket is transitioned to `Done` by the human (`tech-lead`).

---

## 10. What never to do

These are rejected at review with no negotiation:

- Add a code path that reads or writes without `organisation_id`.
- Connect to Postgres as a superuser or `BYPASSRLS` role, or bind the org-GUC at session scope on a pooled connection — both silently void the RLS backstop (ADR-012).
- Bypass the Substrate's ReBAC for a cross-organisation operation.
- Add an upward import (Substrate importing from Capability Registry, etc.).
- Modify tests during implementation to make them pass.
- Use `latest` for a Docker base image or any dependency version.
- Add a credential path that lets Oraclous-the-company staff decrypt customer data in cloud-hosted mode.
- Invoke a capability without writing provenance.
- Merge a PR without explicit reviewer sign-off, or while `customfield_10075` (`needs-human`) is ticked.
- Reproduce verbatim text from a customer's manifest, prompt, or output in error messages, logs, or test fixtures.
- Add or modify ADRs directly — propose to `solution-architect`.
- Edit Confluence architecture pages directly — propose to `solution-architect`.
- Treat a flaky test as "noise" — flakiness is a bug.
- Hand-roll a fetch call from a service when the typed client could be used.
- Write platform code that *is* the harness (rather than interpreting harnesses).
- Read or write the `legacy-reference/` directory's git state — it is a read-only worktree.
- Default to a greenfield rewrite when the story carries a `Lift`, `Reshape`, or `Extract` tag — honour the tag and start from the named legacy source (§11).
- Define a cross-repo data shape, API response, or relation locally — open a `Contract` issue and stop (§11.4).

---

## 11. Legacy reference and the lift-vs-rewrite default

The previous Oraclous backend codebase is available **read-only** at:

```
/Users/reza/workspace/OraclousAI/legacy-reference/old-backend/
```

It is a **git worktree pinned to the `develop` branch** of the previous backend repository. `develop` is the most current branch of that codebase.

### 11.1 This is a migration, not a rewrite

Most existing backend services are production-grade and correctly factored (`auth-service`, `credential-broker-service`) or sprawling-but-salvageable (`knowledge-graph-builder`). The default for backend work is **lift-and-reshape against the four-layer model** — populate the new repo from the legacy service, then refactor under TDD to the target layer and conventions. **Greenfield is the exception, not the default**, applying only to genuinely new surfaces (the application gateway, the metering subsystem) that have no clean legacy precursor.

> The legacy codebase is always at minimum the **behavioural specification** — even when its code is not reusable. New code passes when it does what the legacy did, plus the architectural invariants. "Start from scratch" must be justified, not assumed.

### 11.2 The lift-vs-rewrite rubric

You do not decide lift-vs-rewrite yourself per file. The verdict is decided once per deliverable in the release page's **Migration source map** (see [09. Releases](https://oraclous.atlassian.net/wiki/spaces/OP/pages/164160) Section 7) and arrives in your story brief as a **lift-tag**: `Lift`, `Reshape`, `Extract`, or `Greenfield`, with the specific legacy source path named. Your job is to honour the tag:

- **Lift** — start from the named legacy code, light refactor only.
- **Reshape** — start from the named legacy logic, refit it to the target layer boundary and conventions (organisation_id, OHM, ReBAC, fail-closed), keep the logic.
- **Extract** — lift the behaviour out of a larger legacy service into its target service.
- **Greenfield** — no usable legacy precursor; write fresh against the architecture. The legacy may still be the spec of what *not* to do.

If a story brief lacks a lift-tag for code that you believe has a legacy precursor, that is a planning gap — flag it to `product-planner` (via the coordinator) rather than silently choosing greenfield.

### 11.3 Rules for the legacy reference

- Reference material for behaviour to preserve, read in light of the lift-tag.
- When in doubt: Confluence wins, this `CLAUDE.md` wins, the legacy code is the behavioural reference.
- For a `Greenfield`-tagged story, do not copy legacy directory structure, naming, or service boundaries unless they explicitly match the architecture.
- Never write to `legacy-reference/`. It is a read-only worktree by convention.
- If the worktree appears to be on a branch other than `develop`, that is a setup error — surface it to the human and stop, do not switch branches yourself.

### 11.4 Cross-repo shapes are not yours to define

If you need a data shape, API response, or relation that crosses the repo boundary (anything the frontend also consumes, anything that is a contract between two services), **you do not define it locally**. You open a `Contract` Jira issue with `Agent Owner = solution-architect` and stop, per the [Cross-cutting agreement protocol](https://oraclous.atlassian.net/wiki/spaces/OP/pages/1245185). The shape is decided by `solution-architect` and recorded canonically before either side implements. Defining a cross-repo shape locally is a process violation of the same class as editing tests to make them pass.

---

## 12. Working with Confluence

Before reaching into the web or your training, consult the right Confluence page. The pages live under space `OP` in `https://oraclous.atlassian.net/wiki/spaces/OP/`. Use the Atlassian MCP if available; otherwise the URLs in §2 are direct links.

When you discover that a Confluence page is stale (shipped reality has moved past what it says), open a `docs-writer` ticket; do not edit architecture or ADR pages directly.

---

## 13. Working with Jira

Project key: `ORA`. Cloud ID: `1eb21297-5f52-49a0-a303-3436694b148c`.

| Need | JQL |
| --- | --- |
| My open work | `project = ORA AND "Agent Owner" = "<your-name>" AND status != Done ORDER BY priority DESC` |
| Unassigned work suitable for me | `project = ORA AND "Agent Owner" is EMPTY AND <role-fits-me> AND status = Ready` |
| Needs human attention | `project = ORA AND cf[10075] = "needs-human"` |
| Current sprint backlog | `project = ORA AND sprint in openSprints()` |
| Done this week | `project = ORA AND status = Done AND resolved >= -7d` |

Custom fields used by agents:

- `Agent Owner` — `customfield_10074`, single-select, values are the 11 persona names plus `human` (option id `10031`).
- `needs-human` — `customfield_10075`, multi-checkbox, option id `10032`. Tick to flag, untick to clear.

---

## 14. Working with this file

This file is owned by `docs-writer`. Material changes go through a `[docs]` PR with `docs-writer` as the author and `tech-lead` as the approver. Cosmetic fixes can be batched into a periodic `[chore]` PR.

When you find a gap in this file — something an agent needed and couldn't find — open a `docs-writer` ticket. Do not silently add it; this file is short on purpose.

---

## 15. Resuming after a context reset

If you are resuming work mid-task and have lost prior session context:

1. Read this file.
2. Read your own skill page from [Agent Skills Catalogue](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753852).
3. Read [09. Releases Section 6](https://oraclous.atlassian.net/wiki/spaces/OP/pages/164160) — the canonical Agent Identity Convention. If your skill page's Section 11 disagrees with it on the `needs-human` flag, Section 6 wins (the skill pages have known drift on this point pending `docs-writer` reconciliation).
4. Run the "my open work" JQL above; the ticket with `Agent Owner = you` and `In Progress` status is yours.
5. Read the ticket's comments; the last `[agent:NAME]` comment with an action trailer tells you where you are.
6. Read the linked tests PR (if at Implementation stage) or the brief (if at Tests Authoring).
7. Continue.

If the trail is broken or contradictory, escalate to human via the `escalate_to_human` operation in §5.4.