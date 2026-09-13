# tests/e2e — the deployed-stack suite, through the gateway

Every test here drives the **deployed docker stack** through the application-gateway (`:8006`)
with a real registration and a real JWT: no fakes, no mocks, no service ports, no DB-direct
assertions (`FUCK_CLAUDE_FUCK_PAPERCLIP.md` rules 1, 3, 5). Run it with `scripts/e2e.sh`; the
suite auto-skips when the gateway is down, and a skip is not a pass.

## Two draft-validation gates every hand-built member must satisfy

Any test that builds a team member by hand (a `members[]` entry it POSTs to
`/v1/engine/team-drafts`, `/v1/engine/team-runs`, or a refine op) goes through `validate_draft`
(`packages/ohm/src/oraclous_ohm/compiler/validate.py`). Two rules there block the whole draft
before the test's real subject is reached, and the corpus went red when they landed because no
fixture knew about them (#921). Give every member both:

| Gate | Shipped in | What it needs on the member |
| --- | --- | --- |
| `F-NO-OUTPUT-CONTRACT` | #697 | `"outputs_schema": {"required": ["summary"]}` — the keys it will really put in its answer. Every member, even one nobody depends on. Use `["summary"]` when nothing more specific fits; add `"artifact_refs"` when it persists something. |
| `F-TOOL-UNJUSTIFIED` | #718 | `"tool_rationale": {"<tool>": "why THIS member needs it"}` — one non-empty entry per slug in `tools[]`. A member with `tools: []` needs nothing. |

The minimal shape that passes both:

```python
{
    "role": "writer",
    "kind": "agent",
    "manifest_ref": "org:x/writer@1",
    "subgoal": "write the note",
    "depends_on": [],
    "tools": ["graph-ingest"],
    "outputs_schema": {"required": ["summary"]},
    "tool_rationale": {"graph-ingest": "the writer files its note on the shared graph"},
}
```

`_agent()` in `test_team_draft_loop_gateway_e2e.py` and `_team()` in
`test_saved_team_file_tool_substrate_gateway_e2e.py` are the worked examples.

**Before healing a fixture, read the test.** Some tests build a blocked draft on purpose to prove a
gate fires (`F-CAPABILITY-MISSING`, `F-SUBSTRATE-FILE`, `F-DELIVERABLE-FORMAT-RESERVED`, …). Adding
the two declarations above never removes those verdicts, so a shared helper can carry them — but an
inline shape that exists to be blocked must keep asserting the code it is about, not `would_block`
alone. A test that deliberately proves a gate is never "healed" into passing.

## Markers and legs

| Marker | Leg | Harness | Runs in CI |
| --- | --- | --- | --- |
| `e2e` only | deterministic (`scripts/e2e.sh`) | `HARNESS_LLM_MODE=fake` | yes, every PR |
| `oauth` | real dex provider (`--oauth`) | fake | yes, every PR |
| `byom` + `byom_smoke` | the real-model **subset** (`--byom-smoke`) | `live`, the caller's OpenRouter key | every PR, with the `OPENROUTER_API_KEY` secret |
| `byom` | the **full** real-LLM leg (`--byom`) | `live` | nightly only (`.github/workflows/e2e-nightly.yml`) |
| `github` | real github.com (`--github`) | fake | no (human-gated) |

**A test that binds a model carries `pytest.mark.byom`, not only a `skipif` on the key.** The
`skipif` alone leaves it selected into the deterministic leg whenever the key is in the environment,
where the fake harness cannot produce the real answer it asserts and the run ends FAILED for an
environment reason (this is how the desk tests in `test_apps_platform_default_gateway_e2e.py`
were red, #921).

Verify the harness mode **inside the container** before trusting a run
(`docker compose … exec harness-runtime-service env | grep HARNESS_LLM_MODE`): a deterministic
run against a live model is the stale-environment trap CLAUDE.md warns about.

## `byom_smoke` — the subset a pull request runs (#1012)

The full `byom` leg is ~60 real team runs, several model rounds each: hours of wall clock. It was
cancelled on the job time limit the first day the `OPENROUTER_API_KEY` secret existed, which told
nobody anything. So a pull request runs a **named subset** instead — about five tests, targeted
under ten minutes — and the full leg runs nightly.

The subset is a marker, not a `-k` expression in a workflow file, so changing it is a one-line edit
in a test. Today it is:

| Test | Real-model surface it holds |
| --- | --- |
| `test_byom_real_llm_gateway_e2e.py` | one agent, the user's own stored credential → a real OpenRouter call |
| `test_team_byom_real_llm_gateway_e2e.py` | a team run: engine → Celery worker → live harness, per-member credentials |
| `test_team_run_graph_retrieval_byom_gateway_e2e.py` | a model-issued **tool call** mid-loop, against the bound graph |
| `test_agent_write_citation_gateway_e2e.py` | citation/provenance: what a member writes is cited as `agent` |
| `test_provenance_on_dispatch_gateway_e2e.py` | team-run dispatch: a real capability invocation writes a provenance record (#826) |

`test_compiler_prose_to_team_gateway_e2e.py` (prose → a runnable team) is not here, and it stays that
way: #1043 fixed its JSON peel, but the owner ruled (2026-09-12) that the compiler stays in the
nightly real-model leg only — never the per-pull-request subset — so this file carries `byom` alone.

**When you move the marker, keep those surfaces covered** — a subset that drops tool calling or the
team loop stops being a smoke test of the real-model path. Keep it near five tests: the step has an
explicit `timeout-minutes`, and a subset that outgrows it is a cancelled job again.

Run it locally exactly as CI does:

```
scripts/e2e.sh --up          # the stack, fake harness
scripts/e2e.sh --byom-smoke  # harness → live, the same five tests
scripts/e2e.sh --byom        # the full leg, as the nightly workflow runs it
```

### The owner ruled (2026-09-13): the per-PR subset should stop running at all (#1049)

The exhausted free-model quota (below) made the per-PR `byom_smoke` step fail on every branch for an
environment reason, not a product one. The owner's ruling: "I cannot afford running real e2e test per
PR. the nightly test that takes place daily is enough." This reverses #1012 as implemented by #1013 —
the real-model subset should not run on every pull request at all; the nightly full `byom` leg is
sufficient on its own.

**This is ruled but not yet mechanically implemented.** The obvious way to carry it out — drop
`@pytest.mark.byom_smoke` from the five tests above so `.github/workflows/ci.yml`'s
`-m "byom and byom_smoke"` selection is empty — was deliberately NOT done: that workflow step invokes
`pytest` directly with no handling for exit code 5 ("no tests collected"), so an empty selection
would fail the step for a new, equally confusing reason instead of the old one. Editing that workflow
step is `devops-implementer` territory (`.github/workflows/*`, CLAUDE.md §10), not something a
`[tests]`/docs change here can carry out. So the marker deliberately stays on all five tests for now.
**This is a known, open, tracked gap** (#1049) — either drop or rework the PR-gate step, or make it
tolerate zero-collection, before the marker itself is removed.

### The owner ruled (2026-09-13): fall back to a cheap, strong paid model (#1049)

The default real-model binding was a FREE OpenRouter model whose daily quota ran out for days
straight, failing every real-model e2e test on `main` and every branch with `LLM call → 429` — an
environment failure, not a regression, but one that looked identical to a real one until someone
opened the run body. The owner's ruling: "if nightly failed, use a cheap, but strong enough model
instead." The default is now `openrouter/deepseek/deepseek-v4-flash`; the full reasoning, evidence,
and fallback order live in `tests/e2e/conftest.py` next to `_DEFAULT_E2E_MODEL` — read there rather
than here.

**This changes the default, not what CI or nightly actually run.** `.github/workflows/ci.yml` and
`.github/workflows/e2e-nightly.yml` each hardcode their own `E2E_MODEL` fallback literal
(`openrouter/nvidia/nemotron-3-super-120b-a12b:free`), independent of this file's default — so
nightly keeps hitting the exhausted free tier until the `vars.E2E_MODEL` GitHub Actions repository
variable is set, or those workflow files are edited (`devops-implementer` territory, same as above).
That is a separate, still-open problem this change does not close.

**Validation before trusting deepseek as the default:** a ~20-test representative slice of the full
`byom` marker, run live against the deployed stack with `E2E_MODEL=openrouter/deepseek/deepseek-v4-flash`.
17/20 reached a terminal state (15 PASS, 2 FAIL, 0 ERROR; 3 never terminated within a 45-minute
patience budget — inconclusive, not failures). Zero PRODUCT-class failures and zero classic
weak-model failures (no broken instruction-following, no malformed structured output, no wrong tool
calls). The 2 real failures were both WEAK_MODEL, not PRODUCT: a team run that mechanically succeeded
but scored `0.0` on its own self-judged success criteria (`test_cyclic_team_converges_on_a_real_model_and_lands_artifacts`),
and a Researcher member exhausting its token budget mid-loop in a tool-heavy research team
(`test_the_whole_loop_compile_draft_refine_go_through_the_gateway`). Verdict: cautiously positive,
moderate confidence — an 88% pass rate with no blanket instruction-following breakage, but two real,
narrow weak spots (self-judging harshness, token efficiency under a tool-heavy loop) worth watching.

**What one full run of the `byom_smoke` subset costs:** measured live, one pass of the five scenarios
above (7 harness executions total; one scenario needs 2 member executions plus a tool-call member)
used 5291 input / 1162 output tokens, read off `GET /v1/harnesses/spend`. The service's own rate
table has no entry for deepseek yet (`priced: false`), so the dollar figures below are computed
independently from those raw counts: `deepseek/deepseek-v4-flash` **$0.000501**, versus
`gemini-2.5-flash-lite` $0.000994 and `gpt-4o-mini` $0.001491 for the same pass. Caveat: this likely
slightly under-counts a typical run — the citation/provenance scenario retries up to 3 times when a
weak model answers without calling its tool, and this run's model complied on the first try, so no
retry fired; a less cooperative day could cost up to ~4x more on that one scenario alone.

## Keys a test brings (never a service env)

- `OPENROUTER_API_KEY` and `E2E_MODEL` — from `deploy/.env.test` (untracked); pasted through
  `POST /credentials/`. The default model is a free OpenRouter one (#1000); free models are capped
  at roughly 1000 requests/day per key, so do not loop full BYOM runs.
- `TAVILY_API_KEY` — from `deploy/.env` (the live key, #886); the `deploy/.env.test` key is out of
  credit and only a fallback. A spent key fails as `PROVIDER_QUOTA_EXHAUSTED`, which is the
  environment, not a regression.

## The edge limiter

The suite registers an organisation per test, and the gateway's per-IP window is smaller than that.
`deploy/docker-compose.e2e.yml` (applied by `scripts/e2e.sh` and the CI job, nowhere else) exempts
the host the suite runs from (#850). A `429` on `/v1/auth/register` or `/v1/auth/me` means the stack
was brought up without that overlay; the `register` fixture names the status rather than failing
later with `KeyError: 'organisation_id'`.
