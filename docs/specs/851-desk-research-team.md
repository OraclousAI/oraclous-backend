# Spec: the validation desk's research team (issue #851)

Status: **proposed**. Issue: `OraclousAI/oraclous-backend#851` · Epic: `#827` ·
Client contract: `OraclousAI/oraclous-frontend` `docs/specs/213-decision-brief.md` and `#224`.

---

## Assumptions

Everything here that was not settled on the issue is a decision made in this document and is open
to reversal at review.

1. **The deliverable is a document, not service code.** No Python under `services/` or `packages/`
   changes. What ships is one hand-authored OHM team manifest plus the two scripts that build and
   register it.
2. **The desk is configured with the server-assigned draft id, not the manifest's own id.**
   `POST /v1/engine/team-drafts` mints a fresh id; the manifest's `metadata.id` is not that value.
   Acceptance criterion 1 is therefore met by registering the team and handing the printed draft id
   to whoever sets `VITE_DESK_TEAM_DRAFT_ID`, not by pinning a uuid in the manifest.
3. **The brief travels as an artifact created by `graph-ingest`.** The synthesising member's last
   tool call ingests the brief document; the knowledge-graph service records it as an artifact
   bound to this run (`#728`), and `/v1/artifacts?graph_id=&team_run_id=` is what the desk lists.
   `GET /v1/artifacts/{id}` serves the content verbatim, so what the member writes is what the
   desk's parser reads.
4. **The run's task reaches every member without a per-member declaration.** `task_input` rides
   `TeamRunCreate.inputs[key]` and is delivered verbatim into every member's rendered input, so a
   member with no `inputs[]` still knows what is being validated.
5. **Five members, not six.** Live-web researcher, knowledge scout, cross-examiner, experiment
   designer, synthesiser. The issue allows five or six.
6. **No new capability is written.** The five tools the team declares — `web-research`,
   `graph-ingest`, `knowledge-retriever`, `find-similar`, `webfetch` — are all already seeded.

---

## Objective

A finished run on the validation desk must leave behind a decision brief. Today it leaves member
output that no client can read as one, so the brief screen shows an honest empty state and the
demonstration does not exist.

Success is one real run, driven through the application gateway against a real model, whose newest
parseable artifact the desk renders as a brief.

---

## Tech stack

No additions. The manifest is OHM v1.1 JSON. The two scripts use `httpx` (already a dependency) and
`oraclous_ohm.import_.mapping.build_subharness` — the same function the platform's own importer
calls, so each member's sub-harness carries exactly the capability refs a real import would produce.

---

## Commands

```
uv run python scripts/desk_research_team/build.py                    # print the two documents
uv run python scripts/register_desk_team.py --register "Desk Owner"  # create the draft, print its id
uv run python scripts/register_desk_team.py --token <jwt> --draft-id <uuid>   # replace in place
uv run ruff check scripts && uv run ruff format --check scripts
uv run mypy services packages
scripts/e2e.sh --up                                                  # unchanged, must stay green
```

The proof run itself is a separate deliberate step — `POST /v1/engine/team-runs` through the
gateway on `:8006` — and is not part of either script. Registering a draft calls no model and costs
nothing; running the team costs real tokens.

---

## Project structure

```
scripts/desk_research_team/manifest.json   the committed team manifest — the source of truth
scripts/desk_research_team/build.py        binds the caller's org in, builds each sub-harness
scripts/register_desk_team.py              POSTs or PUTs the draft through the gateway
docs/specs/851-desk-research-team.md       this document
```

Nothing under `services/` or `packages/` is touched, so the layered-architecture and import
contracts are unaffected.

---

## Design

### The five members and the barriers between them

| Member | Runs after | Tools | What it is for |
| --- | --- | --- | --- |
| `researcher` | — | `web-research`, `graph-ingest` | Dated live sources on the core assumptions |
| `knowledge-scout` | — | `knowledge-retriever`, `find-similar`, `graph-ingest` | What the organisation already knows |
| `cross-examiner` | both of the above | `web-research`, `knowledge-retriever`, `graph-ingest` | Tries to break the load-bearing claims |
| `experiment-designer` | `cross-examiner` | `knowledge-retriever`, `webfetch`, `graph-ingest` | The cheapest test for each open question |
| `synthesizer` | all four | `knowledge-retriever`, `find-similar`, `graph-ingest` | Writes the one brief, last |

The first two fan out. Everything after is a barrier, which is what puts the synthesis strictly
after every capture.

### The four constraints the issue names, and where each is honoured

1. **Every member holds at least one real tool.** All five declare tools, and the three that would
   otherwise be pure reasoning — cross-examiner, experiment designer, synthesiser — each read the
   graph through `knowledge-retriever` rather than re-deriving from a prompt. A tool-less member is
   exempt from the grounding check (`#696`) and can fabricate work that poisons everything after it.
2. **The brief is written last.** The synthesiser depends on all four other members, and its
   instruction ends with one ingest call named as the final tool call of the run. The desk opens
   artifacts newest first and stops after six that parse.
3. **Six claims per section.** Stated as a hard limit in the synthesiser's instruction, with the
   explicit direction to drop the weakest rather than split a section to dodge it.
4. **Every capture routes through graph ingest.** Each of the four gathering members ingests the
   source material it read, so the run emits signal between start and settle (`#828`) and the
   research screen has something to show.

### What the synthesiser writes

One JSON object in the shape the client owns. `posture` and `headline` are required and are how the
desk tells a synthesis apart from a captured source. Every other field absent means the
corresponding part of the screen is absent.

The content is the JSON object alone — nothing before it, nothing after — ingested with
`source_type: "json"`. The client also accepts JSON inside the first fenced block of a written page,
but asking for both at once is what the current draft does and it is self-contradictory: a fenced
block is not valid JSON. One instruction, one shape.

The brief carries the honesty the evidence layer cannot yet enforce. A claim the team looked for and
could not establish is labelled `unestablished` with what was looked at and what would settle it. A
guess is labelled `hypothesis` and carries an experiment. `verified` requires more than one
independent source.

### What this deliberately does not wait for

The open evidence-layer gaps — `#808`, `#812`, `#815` (reads that return content with no source
identity), `#789` (nothing checks a cited source supports its claim), `#822` (nothing computes a
number) — are real and none of them blocks this. The claim labels carry the honesty instead. The
corpus decision held on `#827` does not block it either: swapping where evidence comes from later
does not change the team's shape.

---

## Testing strategy

**The proof is a run, not a unit test**, and the issue says so. Three layers, in order:

1. **Document validation, free.** `POST /v1/engine/team-drafts` validates and persists without
   calling a model. A `would_block` of true, or any blocking finding, is a failed build.
2. **A real run through the gateway**, `POST /v1/engine/team-runs`, on the deployed stack rebuilt
   from current `main`, with a real model. A fake-model run is never a proof (rule 8).
3. **The desk's own read**, replayed by hand: list the run's artifacts newest first, open them in
   order, and confirm the first that parses is the synthesiser's and carries both required fields.

The stack currently running was built before `main` moved, so it is rebuilt before the proof run.

---

## Boundaries

- **Always:** every member declares at least one real tool; the synthesiser writes last and exactly
  once; the pre-push gate runs before every push; the run id and the rendered brief are pasted onto
  the issue and the PR.
- **Ask first:** any change to the client's brief schema — the frontend owns it, and a backend-side
  change to it is a cross-repo shape and needs a Contract issue; any new capability; any change
  under `services/` or `packages/`.
- **Never:** a fabricated source or source count; a claim labelled `verified` that one source
  supports; a hardcoded credential or model key in the manifest; a run proven with a fake model.

---

## Success criteria

Numbered against the issue's own acceptance criteria.

| # | Criterion | How it is met |
| --- | --- | --- |
| 1 | The manifest is committed and its id is what the desk is configured with | The manifest is committed; the registered draft id is reported for `VITE_DESK_TEAM_DRAFT_ID` (see assumption 2) |
| 2 | A real gateway run writes an artifact the desk parses as a brief | The proof run, with its id and the rendered brief pasted |
| 3 | Every member declares at least one real tool | All five do; checked in the draft validation report |
| 4 | The synthesiser's artifact is the newest parseable one at settle | Barrier ordering plus the single final ingest call |
| 5 | Claims carry honest labels | `unestablished` and `hypothesis` are specified in the synthesiser's instruction with their required companion fields |

---

## Open questions

1. **Which organisation owns the team?** The register script can mint a fresh user or bind an
   existing token. A demonstration probably wants a stable, named owner rather than a throwaway.
2. **Who sets `VITE_DESK_TEAM_DRAFT_ID`?** It is a frontend deploy-time variable and this repository
   cannot set it. The draft id is handed over on the issue.
3. **How much does one proof run cost?** The budget caps at 500k tokens across the run and 200 tool
   calls. That is a ceiling, not an estimate, and the first real run is what measures it.
