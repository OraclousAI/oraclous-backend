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
