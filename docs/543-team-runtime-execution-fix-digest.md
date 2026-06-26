# Digest — DoefinGPT use-case (#543 / #549): root causes, fixes, what's left

**Audience:** the backend implementer (and solution-architect for the open design call).
**Status:** the imported DoefinGPT team now executes on a real model and its members' outputs
**land on Oraclous and are served via `/v1/artifacts`** — verified end-to-end on a real local
deployed stack (real OpenRouter, the private `Jahankohan/DoefinGPT`). The fix is on the #549 branch
`backend-implementer/543-doefingpt-proof` (commits `17d7f05` + `48a3b1b` on top of #549).

## What was actually broken — three compounding bugs, not one

The CTO real-model verification (6+ runs) found the plumbing was all correct (tool loop, tool
schemas, `graph_id` threading, the graph-ingest connector). The breaks were:

1. **Members didn't execute.** Imported Claude-Code "conductor" agents are written to *propose a
   `## Handoff`* for a human to dispatch. Run inline with only a thin `Objective: <description>`
   turn, a weak model satisfied that persona by emitting a handoff stub (zero tool calls); the loop
   accepted a first-turn no-tool answer as `SUCCEEDED`; and a wide flat stage fanned ~18 members at
   one shared BYOM key, throttle-failing a random member on a capable model.
2. **Every write was silently lost — the artifact-killer.** Members *do* call `Write.ingest`
   correctly, but the graph-ingest tool schema **exposes `graph_id`**, so the LLM hallucinates one
   (often the output filename), and the connector let a tool-call `graph_id` win over the bound
   team graph → the KGS `422`/`404`'d every write → 0 artifacts despite correct calls. The members
   even reported it: *"I am currently unable to save my contributions to the knowledge graph."*
3. **Even a good write wouldn't index/serve** without #549 (an odd `source_type` fails extraction;
   no served surface).

These surfaced *in order* only because the new error-surfacing made each failure legible
(token-budget → 404 model-not-found → ReadTimeout → the 422 write failures).

## The fixes (on the #549 branch)

- **`17d7f05` — team-runtime execution** (`packages/ohm/orchestrate.py`,
  `execution-engine/services/team_run.py`, `harness-runtime/domain/loop/tool_use.py`):
  - `EXECUTION_DIRECTIVE` in `render_member_input` — the member executes now and uses its tools; a
    handoff with no work is not a result.
  - **Completion contract** in the tool loop — a *producing* member (has a graph-ingest `ingest`
    op) that answers with no tool call is nudged once to actually use its tools. Gated on the
    `ingest` op so reasoning/retrieval-only members are unaffected (kept the unit tests green).
  - **Stage concurrency cap** `OHM_TEAM_STAGE_CONCURRENCY` (default 4) so a wide team can't
    self-throttle the shared BYOM key.
  - `make_harness_dispatch` now surfaces the **real harness error** (not a bare `FAILED`) and
    records the child execution id **even on a failed member** (no empty run-tree).
- **`48a3b1b` — graph-ingest: the bound graph wins** (`capability-registry/.../graph_ingest.py`):
  when the run binds a graph it is authoritative; a member can't override it with an invented
  `graph_id`. Writes flip `status=error` → `status=ok`.
- Plus **#549's own** `source_type` normalization + `/v1/artifacts` served surface.

## Verification (real deployed stack, real model, no fakes)

- Members call `Write.ingest` → `status=ok` (was `status=error` / KGS 422).
- **24 artifacts landed, `status=completed`, served verbatim via `/v1/artifacts`, nonce-verified**
  (the per-run nonce is in the served content → genuinely a real model, RULE 8).
- Unit suites green (orchestrate 26 / team_run 66 / tool_use 19); pre-push quality gate green.

## What's left — the one open item (a design decision, not a bug)

The run **terminal state is `FAILED` even when artifacts land**, because the **fail-closed** policy
(ADR-035: "a member whose harness does not SUCCEED fails the team run") aborts the whole team if
**any single member** fails or doesn't converge. With 18 weak-model members, one stalling aborts
the run — the artifacts from the ~17 that succeeded still land, but the run is red.

For a **producing team**, that's arguably too strict. Options (escalate the semantics to
solution-architect/CTO before implementing):
- `return_exceptions=True` on the stage gather so one member doesn't cancel/discard the rest;
- collect-failures + a **partial-success** run state (succeed if the producing path completed);
- per-member bounded retry/backoff on transient provider errors (429/timeout).

## Notes / follow-ups for the implementer

- **Hide `graph_id` from the graph-ingest tool schema when a graph is bound** (cleaner than relying
  on the connector to ignore it — saves the model a wasted hallucinated arg). Optional; the
  connector fix is sufficient for correctness.
- `research-scout` is the **1 of 18** members without a graph-ingest tool; a directive that tells
  *all* members to "Write" makes it loop. The production directive is generic ("use your tools"),
  so it does retrieval — confirm in the production proof.
- The **full unbounded** DoefinGPT (real extensive multi-round analysis) still needs convergence
  work: these agents are built for long human-driven Claude-Code sessions, not a single bounded
  inline execution. The bounded proof confirms the **mechanism**; the durable answer is a real
  **conductor** (`run_team_coordinated`) that issues concrete bounded per-member tasks — an
  architecture decision (deferred here, flagged for solution-architect).
