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

**Slice 2 — full OHM** adds: **atomic reference resolution** of every capability (all-or-nothing) so
an agent gets its full toolset (multi-tool); **canonical serialisation + content hash**; **signature
verification** (Ed25519 / ES256 / RS256) against a config trust store (`HARNESS_OHM_TRUST_KEYS`); and
**`manifest_ref`** — run a registered `kind=harness` descriptor by id. Unsigned OHMs still load (a
*required* signature is a slice-3 policy).

**Slice 3 — governance** ("code wins over prose", Section 6): an OHM's `governance.policy_set_ref`
resolves to a built-in **policy set** (Structured Governance Taxonomy v1.0) that drives coded
enforcement — **signature requirement**, **capability allocation** (allowed registries + forbidden
capabilities) and **BYOM limits** (allowed providers / protocol shapes) at load; and a runtime
**`PolicyEnvelope`** the tool-use loop enforces: **tool-call + wall-time budgets** (→ ESCALATED),
**HITL gates** (capabilities flagged `config.hitl` halt before dispatch), and **output redaction**
(`governance.redact_patterns`). The prompt cannot relax any of it.

**Slice 4 — live LLM (BYOM)** adds the real tool-use loop: `HARNESS_LLM_MODE=live` builds a client
from the OHM model's `protocol_shape` + a **BYOM key resolved via the credential-broker** (ADR-008 —
no platform fallback key; the harness never holds a model key). The **openai-compatible** shape is
wired (OpenRouter serves Claude/OpenAI/Gemini/etc. behind one key); `native`/`gemini` fail closed
until their direct providers land. The OHM names the model as `<provider>/<model-id>` (e.g.
`openrouter/anthropic/claude-sonnet-4`) with `config.credential_id` → the broker credential.

**Slice 5 — human actors + metering** adds: **actor dispatch** (OHM `actors[]`) — a `human`
entrypoint actor halts the run as a **task-board assignment** (`harness_assignments`, status PENDING)
and returns ESCALATED (R4 halts; durable resume is R5), while an `agent` actor (or no actors) runs
the loop; **token-usage metering** (the live client reports `total_tokens`, recorded per run) which
also makes the policy **`max_tokens` budget** enforceable; and read surfaces `GET /v1/harnesses/
executions` (list) + `GET /v1/harnesses/assignments` (the task board).

**Slice 6 — consciousness + sign-off** adds a **consciousness write-through hook**: every run emits a
`consciousness.write` provenance event capturing its outcome (a hook for future consciousness
retrieval — a later capability; deliberately the same provenance write path, not a privileged one).
This completes the R4 build.

## Definition of Done (ORAA-4 §22 — 8 gates)

| # | Gate | Status |
| --- | --- | --- |
| 1 | Structurally conformant (§21 layout + import contracts) | ✅ CI (`structure_enforced: true`) |
| 2 | Not hollow (no stubs/NotImplemented) | ✅ CI `check_no_stubs` |
| 3 | It runs (`docker compose up` healthy, `/health` 200) | ✅ `smoke.sh` step 2 |
| 4 | Real endpoints (no stub/501) vs real substrate | ✅ live e2e + `smoke.sh` |
| 5 | End-to-end smoke vs real substrate | ✅ `smoke.sh` (20 steps, through the gateway) |
| 6 | **Reza personally runs the smoke + signs off** | ⏳ **pending** |
| 7 | `needs-human` until accepted | ⏳ pending |
| 8 | `claimed_done` flipped only after sign-off | ⏳ pending |

**To sign off** (gates 6-8): bring up the stack and run
`bash services/harness-runtime-service/tests/smoke/smoke.sh` (key-free; add `HARNESS_SMOKE_OPENROUTER_KEY`
for the optional live-LLM check). When it passes to your satisfaction, flip
`tools/lint/service_status.yaml` → `harness-runtime-service.claimed_done: true` (which then locks the
no-stubs gate on it forever).

## Smoke

`tests/smoke/smoke.sh` (key-free) runs the full stack on the **fake** LLM and asserts the OHM /
signature / governance behaviour end-to-end. The **live** LLM path (S4) is covered by the unit suite
(OpenAI-compatible marshalling, factory, broker resolution) + a manual OpenRouter run — CI never makes
billable model calls.
