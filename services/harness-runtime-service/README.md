# harness-runtime-service (R4)

The platform's execution core. It loads an **OHM** (Oraclous Harness Manifest), dispatches its actor,
and runs the agent **plan→act→observe** tool-use loop — each tool call is dispatched to the
capability-registry's real execute, results are fed back, and the loop iterates to an answer under a
budget, writing provenance every step.

Layered per the service-architecture standard (`routes → services → domain → repositories → core`). It composes the other
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

## Tool dispatch: the runtime binds the operation, never the model (#956)

Every LLM-callable tool is one capability **operation** (`<binding>__<operation>`, built by
`domain/tool_schemas.py`). Which operation runs is decided by **which tool the model called** — the
`ToolSpec.operation` the runtime bound — and never by an argument. The rule, shared by both dispatch
paths, is:

1. **At dispatch** (`services/harness_execution_service.py` → `domain/tool_schemas.dispatch_payload`)
   the registry payload is `{"operation": spec.operation, **args}` with the model's own `operation`
   key removed first. A key equal to the bound operation is stripped and the call proceeds. A key that
   differs — any value, any type; the **value** is matched exactly, with no case folding — raises
   `OperationOverrideRefused` **before the registry is called**: the loop feeds it back to the model as
   a coded tool error (`operation_override_refused`, no echo of the supplied value) and the run goes
   on; the service logs the attempt at WARNING with at most 64 characters of the value. The **key** is
   matched case-INsensitively (#1004 item 3), so `Operation` / `OPERATION` are the operation key too,
   and a payload carrying two spellings that disagree is refused rather than resolved by ordering. The
   strip is shallow: a nested `operation` is the tool's own argument. `args` that is not a JSON object
   raises the sibling `NonObjectArgumentsRefused` (#1004 item 4) — both share a `ToolDispatchRefused`
   base — rather than a bare `TypeError` from the payload builder.
2. **At the schema** a first-party operation's schema carries `additionalProperties: false`. This is a
   **hint to the provider, not a server-side check**: nothing in this service re-validates a model's
   arguments against the schema, so a schema-honouring provider refuses an undeclared key before the
   runtime sees it and a provider that ignores the hint sends it straight through. `dispatch_payload`
   passes such a key on unchanged and logs it at WARNING by **name** — never its value, at most 5
   names, at most 64 rendered characters — and only for a schema that closed itself (#1004 item 2).
   Real argument enforcement is **#898** (strict schemas) and **#911** (`required`); until those land,
   read "closed schema" as advisory. An imported MCP operation's schema is the **server's** contract
   and is passed through as it came (#698 D1), open or closed; its no-schema fallback stays open, and
   an open schema declares extra keys legal, so nothing is reported for it.
3. **At the connector** the imported-server path strips `operation` again in the registry's mcp
   connector (#698 D3) because the key means nothing to a third-party server; a first-party connector
   that does not implement the operation answers `unsupported operation '<name>'` with the echoed name
   capped at `executors.base.TOOL_ERROR_CHARS` (300) — the same bound the mcp connector caps a tool's
   own error text at, one constant for both paths.
4. **At the registry** (#1004 item 1, defence in depth) `ToolExecutionService` checks the requested
   `operation` against the operations the **instance's descriptor** declares (`spec.capabilities`,
   read by `domain/operations.py`) before any executor is created, and refuses an undeclared one with
   a coded 409 (`unsupported_operation`) whose whole message is bounded by `TOOL_ERROR_CHARS`. A call
   with **no** `operation` is not refused: the connector's own default stands, and that default is
   connector code rather than caller input. An imported MCP instance is checked too, not exempt — its
   declared set is the single server tool name it was imported and approved as.

So the internal path and the imported path **agree**: the harness enforces the binding once for every
tool, the registry re-checks it against the descriptor without trusting the harness, and neither path
reflects unbounded model text into a persisted error. Known limit: an imported MCP tool whose own input
schema declares a parameter named `operation` cannot receive it (rule 1 refuses a differing value; the
mcp connector strips it anyway).

## Definition of Done (8 gates)

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
