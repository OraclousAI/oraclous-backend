# harness-runtime-service (R4)

The platform's execution core. It loads an **OHM** (Oraclous Harness Manifest), dispatches its actor,
and runs the agent **plan→act→observe** tool-use loop — each tool call is dispatched to the
capability-registry's real execute, results are fed back, and the loop iterates to an answer under a
budget, writing provenance every step.

Layered per ORAA-4 §21 (`routes → services → domain → repositories → core`). It composes the other
services over HTTP (it never imports them): the **capability-registry** (resolve capability → instance
→ execute) and, from slice 4, the **credential-broker** (BYOM model creds).

## Build status — slice 1 (runnable core)

- OHM v1 thin load + validate (`domain/ohm/`); entrypoint cross-checked to a declared capability.
- The capability-agnostic tool-use loop (`domain/loop/tool_use.py`) over a pluggable LLM seam
  (`domain/llm/`). Slice 1 ships the **key-free fake** client; real protocol shapes (native /
  openai-compatible / gemini-compatible) + BYOM land in slice 4.
- Dispatch resolves each OHM capability binding → a registry instance and calls the **real**
  `/api/v1/instances/{id}/execute` (identity propagated per ADR-018).
- Durable Postgres store: `harness_executions` + a provenance sink behind the substrate collector.
- `POST /v1/harnesses/execute`, `GET /v1/harnesses/executions/{id}`, `GET /health`. Port `8007:8000`.

Later slices: full OHM + signatures + atomic refs (S2); governance/policy/budget (S3); live Anthropic
+ BYOM (S4); human-actor dispatch (S5); consciousness hook + §22 sign-off (S6).

## Smoke (key-free)

`tests/smoke/smoke.sh` runs the full stack, submits an OHM whose agent calls the **real** PostgreSQL
Reader against the stack's own Postgres, and asserts a real result + a provenance trail.
