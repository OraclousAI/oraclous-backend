# Retired: WorkflowService and PipelineGenerator

| Field | Value |
|---|---|
| **Status** | Retired |
| **Retirement date** | 4 June 2026 |
| **Retired by** | Workflow-concept retirement (see ADR-005 below) |
| **ADR reference** | [ADR-005 — Workflow Concept Retirement; Harness as Replacement](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753772) |
| **Replacement** | Harness runtime (OHM documents + capability composition); harness orchestrator capabilities ship in R4 |

## What these modules were

`oraclous-core-service/app/services/workflow_service.py` and `oraclous-core-service/app/services/pipeline_generator.py` were introduced in August 2025 as part of the v0 two-concept model: _agents_ (leaf-level actors) and _workflows_ (explicit orchestrations of agents with edges, conditional branches, and shared state).

**`WorkflowService`** provided business logic over a `WorkflowRepository`: generating workflows from a natural-language prompt (LangGraph placeholder), validating a workflow structure, executing a workflow (creating an execution record and marking it for a job processor), and creating a workflow from a saved template. It held four public methods — `generate_from_prompt`, `validate_workflow`, `execute_workflow`, `create_from_template` — all wrapping placeholder or stub logic with no production callers outside its own routes module.

**`PipelineGenerator`** provided three stubs intended for a future LangGraph integration: `generate_workflow` (construct a `Workflow` schema instance from a prompt and context dict), `suggest_tools` (return a list of tool-definition IDs matching given requirements), and `optimize_workflow` (return a modified, optimised workflow). None of the three stubs were wired to any caller; every method body was a comment block labelled "Placeholder for LangGraph integration."

`oraclous-core-service/app/api/v1/endpoints/workflow_routes.py` was also removed at the same time: it was the sole public import site of `WorkflowService` and exposed the workflow CRUD and execution HTTP endpoints. Removing it and its `include_router` entry from `app/api/v1/router.py` completed the surface removal.

## Why retired

The v0 two-concept model (agent + workflow) created structural, recurring costs:

- **Two governance evaluations** on every composed execution, with no canonical rule for which surface wins when they disagree.
- **Two audit streams** that observers had to correlate to reconstruct a single execution.
- **Two budget surfaces** that frequently disagreed on what counted.
- **Two failure-mode catalogues** that overlapped in practice.

The split also failed at cross-service and cross-organisation composition: the outer workflow's identity dominated governance inconsistently depending on which service held the call.

ADR-005 concluded that the workflow-vs-agent distinction is an orchestration concern, not a platform-model concern. From the platform's point of view, both are the same kind of thing — a binding of capabilities, models, prompts, governance, and runtime metadata that can be executed and audited as a unit. The orchestration logic (run X, then Y based on the output of X) belongs _inside_ the harness as a capability composition, not _above_ the harness as a separate concept.

With no production callers, no shipped data in the workflow tables (per the ADR-005 implementation note: no workflow table exists in oraclous-core-service), and the v0 model confirmed as structurally redundant, the services were retired entirely rather than migrated.

## What replaces them

The platform has **one first-class actor concept: the harness**, described by an [OHM document](https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501).

Composition that v0 expressed as a workflow is expressed in v1 as a **harness whose entrypoint capability orchestrates other capabilities** — including, when needed, other harnesses referenced through the federated or organisation-private registries. There is one governance evaluation, one audit stream, and one budget surface: the harness's.

The platform ships a set of baseline orchestrator capabilities in `core` (sequential, parallel, conditional) as part of **R4**. Until R4, composed execution is authored directly in harness entrypoint capabilities.

## Verified by

The deletion was verified by six structural unit tests in `oraclous-core-service/app/tests/unit/test_workflow_retirement.py` (W01–W06), covering file deletion, importer removal, router de-registration, and full import-graph grep-clean. All six pass on `main` as of the retirement date.

## See also

- [ADR-005](https://oraclous.atlassian.net/wiki/spaces/OP/pages/753772) — full decision record, alternatives considered, and consequences
- [OHM v1.0 Specification](https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501) — the harness descriptor format
- [ADR-003](https://oraclous.atlassian.net/wiki/spaces/OP/pages/884737) — platform-as-code, actors-as-harnesses (the framing ADR-005 specialises)
- [Architecture Revision History](https://oraclous.atlassian.net/wiki/spaces/OP/pages/426111) — revision entry for this retirement
