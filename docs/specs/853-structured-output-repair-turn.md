# Spec: one bounded repair turn for a malformed structured document (issue #853)

Status: **proposed**. Issue: `OraclousAI/oraclous-backend#853` · Epic: `#827` · Child of #851's first run.

---

## Assumptions

Everything here not settled on the issue is a decision made in this document, open to reversal at
review. Three were ruled directly on the issue (2026-08-21) and are restated, not reopened:

1. **Structured declaration is a new field on the member**, not inferred from prose. It lives on
   `OHMMember` (`packages/ohm/src/oraclous_ohm/manifest.py:152`) beside the existing
   `outputs_schema: dict[str, Any]` field. That field already exists but is validated only at a
   member→member hand-off (`oraclous_ohm.envelope.build_handoff`, `validate_payload`) — it checks
   that required *keys* are present in an already-parsed dict. It is never consulted for a terminal
   member (nothing depends on it, so `build_handoff` never runs) and never catches a payload that
   fails to parse as JSON at all, which is issue #853's actual failure. The new field this spec adds
   answers a narrower question than `outputs_schema` does: not "what shape", but "must this parse".
2. **The repair happens inside the member's own turn**, in the tool-use loop
   (`services/harness-runtime-service/.../domain/loop/tool_use.py::run_tool_use_loop`), not in the
   knowledge-graph-service's ingest worker. That worker path is also ruled out on a technical
   ground the issue didn't have: `graph-ingest` is fire-and-forget. The connector
   (`services/capability-registry-service/.../domain/connectors/graph_ingest.py`) POSTs to
   `/internal/v1/ingest` and gets back `{job_id, status}` — the body is parsed later, by a worker,
   possibly after the run has already settled. Catching the error there is both the wrong layer
   (per the issue's own reasoning) and too late (the run may be done by the time the worker runs).
3. **The repair call is granted on top of the member's budget**, not charged to it. Implementation:
   the retry call's tokens still count toward `tokens_used`/the pooled ceiling (so cost is never
   hidden), but the *iteration cap* and the *tool-call cap* that would otherwise stop the member are
   each allowed one extra step reserved for exactly this repair, mirroring how `_REPAIR_ATTEMPTS`
   works for the compiler's reviewer loop (`packages/ohm/src/oraclous_ohm/compiler/team.py:108`).
4. **The check runs synchronously, before the tool call is recorded as a successful step**, catching
   it at the same point the loop already inspects a tool result — the same place citation-correction
   (`_citation_correction`, `tool_use.py:213`) and the `#743` completion-nudge already sit. No new
   phase is added to the loop; this extends the existing per-tool-call inspection.
5. **Validation is `json.loads`, nothing more.** The issue's failure is a syntax error
   (`Expecting property name enclosed in double quotes`), not a schema violation. This spec does not
   also run the member's `outputs_schema` check inline — that stays where it is, at hand-off. A
   member with no downstream consumer and a passing `json.loads` is unaffected by `outputs_schema`
   either way, exactly as today.
6. **Scope is the `graph-ingest` tool call whose declared content is structured.** Other tools
   (`web-research`, `knowledge-retriever`, `find-similar`, `webfetch`) do not write a document this
   fix is about; they are untouched.

---

## Objective

A team member whose declared output must be structured gets one bounded chance, inside the same run,
to fix a document that fails to parse — instead of the run losing every member's work over one
malformed character. Success is issue #853's own scenario, rerun: a synthesizer member that would
have written the same broken bracket instead corrects it in one extra turn, and the run settles
SUCCEEDED with a readable brief.

---

## Tech stack

No additions. Touches three existing packages/services along the layer they already own:

- `packages/ohm` (manifest schema — add the declaration field, `resolve_member_caps`-adjacent).
- `services/harness-runtime-service` (domain layer — the retry hook in `tool_use.py`).
- `services/capability-registry-service` (domain layer — nothing structural; the connector's
  existing tool-call result is what the loop inspects, no connector change expected).

---

## Commands

```
uv run pytest packages/ohm/tests -k structured_output
uv run pytest services/harness-runtime-service/tests -k repair
uv run ruff check packages/ohm services/harness-runtime-service
uv run mypy packages/ohm services/harness-runtime-service
```

---

## Design

### 1. Declaring a structured member

Add to `OHMMember` (`packages/ohm/src/oraclous_ohm/manifest.py`):

```python
requires_valid_json: bool = False  # #853: this member's last tool call must carry parseable JSON
```

Placed beside `outputs_schema`, not folded into it — `outputs_schema` is about shape (required
keys) and is checked at hand-off; this is about syntax (does it parse at all) and is checked at the
tool call. A member can carry either, both, or neither.

### 2. The check point

In `run_tool_use_loop`, the loop already inspects each tool call's result (`_run_tool_calls`,
`tool_use.py:389`) and already has a precedent for turning a failed check into a corrective message
fed back to the model (`_citation_correction`). Add, gated on `member.requires_valid_json` and the
tool call being a `graph-ingest` ("ingest") operation whose `source_type` is `"json"`:

- Attempt `json.loads(content)` on the call's `content` argument before recording the step `ok`.
- On success: record the step as today, no behaviour change.
- On failure, **once per member run** (a new local counter, `repair_used: bool`, mirroring how
  `nudged` already gates the one-time completion nudge at `tool_use.py:296`):
  - Do not record the tool call as a successful ingest.
  - Append an assistant/tool-error turn quoting the parser's own message verbatim (e.g.
    `Expecting property name enclosed in double quotes: line 1 column 2541 (char 2540)`), asking the
    member to re-emit the corrected document and call `graph-ingest` again.
  - Grant one additional iteration and one additional tool call beyond the member's caps for this
    turn only (assumption 3), so a budget-exhausted member still gets the repair.
  - Set `repair_used = True`.
- On a **second** failure (`repair_used` already `True`): record the step as a normal tool error and
  let the loop's existing failure handling run unchanged — no second repair, no loop.

### 3. Settling the run

No change to `team_run_service.py` or `orchestrate.py`'s settle logic — a member that repairs
successfully produces an ordinary `ok` step and the run settles SUCCEEDED exactly as any other
successful member would. A member that fails its one repair also fails exactly as today (the team
run's existing fail-closed behaviour per `team_run.py`'s docstring: "A member whose harness does not
SUCCEED fails the team run").

---

## Testing strategy

Per this repo's TDD contract (`CLAUDE.md` §4.1): a `[tests]` PR lands first with failing tests, then
an `[impl]` PR turns them green.

- **`packages/ohm/tests`**: `OHMMember(requires_valid_json=True)` round-trips; default is `False`
  (back-compat, every existing manifest keeps behaving as today).
- **`services/harness-runtime-service/tests`** (unit, the loop): a fake `graph-ingest` tool call
  returning malformed JSON content triggers exactly one corrective turn with the real
  `json.JSONDecodeError` message quoted; a second malformed call fails the member with no further
  retry; a member with `requires_valid_json=False` (or a non-JSON `source_type`) is never checked; a
  member at its tool-call cap still gets the one extra repair call.
- **Deployed-stack e2e (rule 1b/DoD gate)**: rerun issue #853's own scenario — a manifest with
  `requires_valid_json=True` on the synthesizer, driven through the gateway against a real model,
  proving a real (not hand-broken-fixture) malformed-JSON turn gets repaired and the run settles
  SUCCEEDED. `HARNESS_LLM_MODE=fake` never satisfies this per the DoD law — needs a real model leg.

---

## Boundaries

- **Always**: keep the repair to exactly one extra turn; keep `outputs_schema` hand-off validation
  unchanged; keep the retry's tokens counted toward the pooled budget.
- **Ask first**: extending this to non-`graph-ingest` tools, or to the ingest worker's async path
  (both explicitly out of scope per the assumptions above).
- **Never**: charge the repair call to the member's own exhausted budget cap; loop past one repair;
  silently swallow a second failure as success.

---

## Success criteria

1. `OHMMember.requires_valid_json` exists, defaults `False`, is back-compatible.
2. A `graph-ingest` call with `source_type: "json"` and malformed content, from a member with
   `requires_valid_json=True`, gets exactly one corrective turn quoting the real parser error.
3. A second malformed attempt settles the member (and thus the run) exactly as today — no loop.
4. Proven on a real run through the gateway on a real model (issue #853's own scenario), not only a
   unit test with a hand-broken fixture.

## Open questions

None outstanding — the three the issue raised are ruled in Assumptions 1–3. Flag at review if the
`graph-ingest`-only scope (assumption 6) should widen before this ships.
