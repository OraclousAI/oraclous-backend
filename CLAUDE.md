# CLAUDE.md — oraclous-backend

This file is the working contract for any AI agent (Claude Code, an agent in the harness runtime, or otherwise) operating in this repository. Read it in full at the start of every session.

This repo is **`OraclousAI/oraclous-backend`** — the Python codebase for the Oraclous Platform: substrate, capability registry, harness runtime, execution engine, application gateway, and the supporting services that back them. The repo is currently empty by design; the scaffolding work in R0.5 produces its initial shape.

---

## 1. Identity and scope

This repo is owned by three implementer agents and reviewed by four others:

| Agent | Activity here |
| --- | --- |
| `backend-implementer` | Authors all production Python code |
| `test-author` | Authors tests *before* implementation in a separate PR |
| `devops-implementer` | Owns `Dockerfile`, `docker-compose.yml`, Helm charts, GitHub Actions, observability config (lives in this repo for backend services) |
| `qa-engineer` | Verifies test suite, coverage, flakiness; authors regression tests under `tests/` |
| `code-reviewer` | Always on every PR for craft review |
| `security-architect` | On every security-marked, infra, or credential-touching PR |
| `solution-architect` | On every architecture-touching PR; reviews tests-only PRs at the Tests Review gate |
| `docs-writer` | Reads merged PRs; updates Confluence; drafts release notes |

`tech-lead` (the human, Reza Jahankohan) is the final sign-off on every gate that requires human approval. See **§8 Gates** below.

The full agent skill definitions live in Confluence under [Agent Skills Catalogue](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753852). Read your own skill page on session start.

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

Reference: [ADR-006 — Organisation as Outermost Tenancy Unit](https://oraclous.atlassian.net/wiki/spaces/OP/pages/393403).

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

The full convention lives in [09. Releases Section 6](https://oraclous.atlassian.net/wiki/spaces/OP/pages/164160). The operational summary for this repo:

1. **Every Jira ticket I touch carries the `Agent Owner` custom field**, set to my agent name while I am working on it. When I hand off, I set it to the receiving agent's name.

2. **Every comment, worklog, and PR description I write begins with `[agent:NAME]`**, where NAME is my agent persona. The prefix is the only way the audit trail can attribute the comment to me rather than to the human whose account I share.

3. **My open work query** (substitute NAME):
   ```jql
   project = ORA AND "Agent Owner" = "NAME" AND status != Done ORDER BY priority DESC
   ```

4. **Handing off:** set `Agent Owner` to the target agent, transition status, post a comment beginning `[agent:NAME]` and ending with a structured trailer:
   ```
   ---
   agent: NAME
   action: handoff_to
   to: target-agent-name
   ```

5. **Escalating to human:** set `Agent Owner = human`, add the `needs-human` label, post a comment with action `escalation` and the reason.

6. **Spike, observation, completion, review request:** each has an action trailer (`observation`, `complete`, `review_request`). See 09. Releases Section 6.3 for the full operation set.

This is enforced by skill discipline through R6. From R7 onward it is enforced by a Capability Registry entry — the standalone agent-MCP server listed as an R7 deliverable.

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
├── packages/                       # shared libraries (substrate primitives,
│   ├── ohm/                        #   OHM types and validators,
│   ├── substrate/                  #   ReBAC client, provenance collector,
│   ├── governance/                 #   organisation_id propagation utilities,
│   ├── provenance/                 #   telemetry, errors, logging)
│   ├── rebac/
│   ├── telemetry/
│   └── errors/
├── services/                       # one directory per service; matches
│   ├── auth-service/               #   the names in 04. Services Reference.
│   ├── credential-broker-service/  #   Each service has:
│   ├── knowledge-graph-service/    #     - src/<service_name>/
│   ├── knowledge-retriever-service/#     - Dockerfile
│   ├── capability-registry-service/#     - pyproject.toml
│   ├── harness-runtime-service/    #     - README.md (operator-facing)
│   ├── execution-engine-service/   #
│   └── application-gateway-service/
└── tests/                          # cross-service integration tests
    ├── integration/
    ├── security/
    ├── isolation/
    ├── byom/
    └── organization_isolation/
```

Per-service unit tests live alongside the service code under `services/<name>/tests/`. Cross-service tests live at the repo root under `tests/`.

### 6.1 The `packages/` directory is shared infrastructure

Code in `packages/` is consumed by multiple services. It is the only horizontal dependency in the repo. Adding a new package is a non-trivial architectural decision and requires `solution-architect` approval.

### 6.2 Service directories are vertical

A service owns its own code, its own tests, its own Dockerfile, and its own operator-facing README. Cross-service coupling goes through `packages/` (for libraries) or through service APIs (for runtime calls).

---

## 7. Services

The eight backend services described in [04. Services Reference](https://oraclous.atlassian.net/wiki/spaces/OP/pages/786433). Each has its own Confluence page; consult it before doing any work in that service's directory.

| Service | Layer | Confluence | Brought into target shape in |
| --- | --- | --- | --- |
| `auth-service` | Substrate | [Page 622756](https://oraclous.atlassian.net/wiki/spaces/OP/pages/622756) | R1 |
| `credential-broker-service` | Substrate | [Page 753812](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753812) | R1 |
| `knowledge-graph-service` | Substrate | [Page 753832](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753832) | R3 |
| `knowledge-retriever-service` | Substrate | [Page 622776](https://oraclous.atlassian.net/wiki/spaces/OP/pages/622776) | R3 |
| `capability-registry-service` | Capability Registry | [Page 884757](https://oraclous.atlassian.net/wiki/spaces/OP/pages/884757) | R2 |
| `harness-runtime-service` | Harness Runtime | [Page 688350](https://oraclous.atlassian.net/wiki/spaces/OP/pages/688350) | R4 |
| `execution-engine-service` | Harness Runtime | [Page 884777](https://oraclous.atlassian.net/wiki/spaces/OP/pages/884777) | R5 |
| `application-gateway-service` | Application Gateway | [Page 131124](https://oraclous.atlassian.net/wiki/spaces/OP/pages/131124) | R6 |

Some services already exist in legacy form in the previous codebase and are being lifted into target shape phase by phase. Read [Section 8 — Consolidation and Migration Plan](https://oraclous.atlassian.net/wiki/spaces/OP/pages/688329) before touching any service to understand which phase you are in.

---

## 8. Gates

Every story passes through these gates. The agent that owns each transition is named.

| From | To | Owner | What's verified |
| --- | --- | --- | --- |
| Backlog | Ready | `product-planner` + `solution-architect` (arch review) + `security-architect` (threat tags) | Brief is testable; architecture references present; threat tags set |
| Ready | Tests Authoring | `test-author` | Pickup |
| Tests Authoring | Tests Review | `test-author` | `[tests]` PR opened with failing tests |
| Tests Review | Implementation | `solution-architect` + `security-architect` (if security-marked) | Tests assert the right boundary; security tests genuinely exercise threats; merge `[tests]` PR |
| Implementation | Code Review | implementer | `[impl]` PR with green tests |
| Code Review | Done | `code-reviewer` + `qa-engineer` + any required architect + `tech-lead` (final) | Craft, coverage, security, architecture all signed off; merge `[impl]` PR |

Reference: [Definition of Done](https://oraclous.atlassian.net/wiki/spaces/OP/pages/66010).

---

## 9. Done means done

A story is **done** when, and only when:

1. Tests PR merged; implementation PR merged; both passed full CI.
2. All gates have been transitioned through in order; no skips.
3. Every required reviewer signed off explicitly (no silent approvals).
4. `Agent Owner` is set to whoever last touched it (typically `tech-lead` after merge).
5. Coverage on new code is adequate; no new flaky tests; no regressions in the full suite.
6. If service behaviour changed: `docs-writer` has updated the affected service reference page or has an open ticket to do so within the sprint.
7. If architecture-significant: a follow-up ADR is open if any architectural decision crystallised.
8. The Jira ticket is transitioned to `Done` by the human (`tech-lead`).

---

## 10. What never to do

These are rejected at review with no negotiation:

- Add a code path that reads or writes without `organisation_id`.
- Bypass the Substrate's ReBAC for a cross-organisation operation.
- Add an upward import (Substrate importing from Capability Registry, etc.).
- Modify tests during implementation to make them pass.
- Use `latest` for a Docker base image or any dependency version.
- Add a credential path that lets Oraclous-the-company staff decrypt customer data in cloud-hosted mode.
- Invoke a capability without writing provenance.
- Merge a PR without explicit reviewer sign-off, or while the `needs-human` label is set.
- Reproduce verbatim text from a customer's manifest, prompt, or output in error messages, logs, or test fixtures.
- Add or modify ADRs directly — propose to `solution-architect`.
- Edit Confluence architecture pages directly — propose to `solution-architect`.
- Treat a flaky test as "noise" — flakiness is a bug.
- Hand-roll a fetch call from a service when the typed client could be used.
- Write platform code that *is* the harness (rather than interpreting harnesses).

---

## 11. Working with Confluence

Before reaching into the web or your training, consult the right Confluence page. The pages live under space `OP` in `https://oraclous.atlassian.net/wiki/spaces/OP/`. Use the Atlassian MCP if available; otherwise the URLs in §2 are direct links.

When you discover that a Confluence page is stale (shipped reality has moved past what it says), open a `docs-writer` ticket; do not edit architecture or ADR pages directly.

---

## 12. Working with Jira

Project key: `ORA`. Cloud ID: `1eb21297-5f52-49a0-a303-3436694b148c`.

| Need | JQL |
| --- | --- |
| My open work | `project = ORA AND "Agent Owner" = "<your-name>" AND status != Done ORDER BY priority DESC` |
| Unassigned work suitable for me | `project = ORA AND "Agent Owner" is EMPTY AND <role-fits-me> AND status = Ready` |
| Needs human attention | `project = ORA AND labels = needs-human` |
| Current sprint backlog | `project = ORA AND sprint in openSprints()` |
| Done this week | `project = ORA AND status = Done AND resolved >= -7d` |

Custom fields used by agents:

- `Agent Owner` (single-select) — current owner; values are the 11 agent names plus `human`.

---

## 13. Working with this file

This file is owned by `docs-writer`. Material changes go through a `[docs]` PR with `docs-writer` as the author and `tech-lead` as the approver. Cosmetic fixes (typos, broken links) can be batched into a periodic `[chore]` PR.

When you find a gap in this file — something an agent needed and couldn't find — open a `docs-writer` ticket. Do not silently add it; this file is short on purpose.

---

## 14. Resuming after a context reset

If you are resuming work mid-task and have lost prior session context:

1. Read this file.
2. Read your own skill page from [Agent Skills Catalogue](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753852).
3. Run the "my open work" JQL above; the ticket with `Agent Owner = you` and `In Progress` status is yours.
4. Read the ticket's comments; the last `[agent:NAME]` comment with an action trailer tells you where you are.
5. Read the linked tests PR (if at Implementation stage) or the brief (if at Tests Authoring).
6. Continue.

If the trail is broken or contradictory, escalate to human (`Agent Owner = human`, label `needs-human`).