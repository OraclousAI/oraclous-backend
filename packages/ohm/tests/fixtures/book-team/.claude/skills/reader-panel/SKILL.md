---
name: reader-panel
description: >
  Team ① synthetic reader panel for _The Future of Consciousness_ — a MiroFish-style wind tunnel.
  Builds a data-grounded roster of LLM reader agents from comp-book review themes, then flies a
  chapter / part / the full book through them to surface a variance-first, ranked-objection report.
  DIRECTIONAL ONLY: never gates, locks, advances, or publishes; subordinate to the real ARC program
  (AGENTS.md §5). Use when the author says "reader panel", "run the panel", "how would readers react
  to CH-NN / Part-I / the book", or asks for a synthetic pre-screen before real ARC readers.
---

# reader-panel — the synthetic wind tunnel

**First read [`AGENTS.md`](../../../AGENTS.md)** and `teams/1-insight/charter.md`. This is a
**directional instrument, not a predictor.** It tells you where readers likely get bored,
confused, or split — and never forecasts reception, never gates a chapter, never replaces real
readers. Pair it with the real ARC program (`arc-manager`).

The engine is the `reader-panel` Python package at `reader-panel/` (the `reader_panel` module).
This skill is the command surface; it shells out to the CLI and relays the report.

## Setup (once)
```bash
cd reader-panel && uv venv --python 3.12 && uv pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...      # real runs need this; omit only for --mock
```

## Subcommands

### `corpus` — gather the review-theme corpus (one-time / refresh)
Run `reader-panel corpus`. Web-searches the niche's comp titles (Harari, Bostrom, Chalmers,
Tegmark, Seth, Suleyman, …) for **paraphrased** Resonance/Complaint/Emotion themes — no verbatim
reviews, no PII — into `research/raw/panel-corpus/` with provenance. Folds in existing
`calib-reader-voice` / `calib-audience` artifacts if present (reuse Team ①'s work).

### `personas` — freeze the weighted roster (one-time / refresh)
Run `reader-panel personas`. LLM-mines the corpus into ~30 grounded archetypes, each with a
SIGNATURE complaint, a job-to-be-done, a reading level, and a panel weight (the roster mirrors the
real readership mix; within-segment variance is preserved). Writes `research/panel/roster/` +
`roster.json`. **Commit the roster** so runs stay comparable; re-mine only when the corpus or the
target readership changes.

### `run <scope>` — fly a chapter/part/book through the panel
Run `reader-panel run --scope <CH-NN | Part-I | full>` (builds corpus+roster first if missing).
- `--rounds 1` (default): every persona reads the chapter **privately** (the real reading mode).
- `--rounds 2`: adds the **word-of-mouth** layer — a peer feed, then bounded-confidence
  re-reaction (`--peer-feed full|echo|mixed`). This is the MiroFish contribution; use it to see
  whether a chapter polarizes or builds consensus once readers influence each other.
Output: `research/panel/reports/panel-<scope>.md` (headline = ranked objections, each tagged
**fix** vs **protect**; distribution + polarization + the harshest minority verbatim) and the full
run under `research/panel/runs/`.

### `--mock` — offline dry run
Append `--mock` to any command to exercise the whole pipeline with deterministic canned data and
no API key. For seeing the report shape only — **not** signal.

## How `book-studio` calls this
`book-studio panel <scope>` routes here. Run the CLI, read the report, and relay to the author:
the ranked objections, the distribution/polarization, the harshest minority, and the variance
flag — always labeled SYNTHETIC. **Stop there.** Nothing advances; the author decides.

## Operating rules
- Writes only under `research/panel/` and `research/raw/panel-corpus/`. Never `bible/`, `drafts/`,
  `outline/`, `qa/`, or a chapter's `status`/lock.
- **No chapter locks (Gate C) or manuscript approval (Gate D) on synthetic signal alone** — ≥5 real
  ARC/beta reactions are required (AGENTS.md §5). Say this whenever a lock is in view.
- For objections tagged *challenging → protect* (and anything in `research/panel/edge-ledger.md`),
  never recommend cutting/softening on the panel's say-so — surface it as a deliberate edge.
- Validate before trusting: run a validation protocol (predict 10–30 real ARC reactions before seeing
  them; keep the panel only where it beats a coin flip on direction).
