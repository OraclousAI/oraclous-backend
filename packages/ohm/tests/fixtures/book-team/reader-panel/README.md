# reader-panel — a synthetic reader panel for the Book Studio

A MiroFish-style **wind tunnel** for _The Future of Consciousness_. Data-grounded
LLM reader agents read a chapter, react in character, optionally influence each
other (word-of-mouth), and produce a **variance-first, ranked-objection** report.

> **This is a directional instrument, not a predictor.** Its validated ceiling is
> roughly one real reader's two-week self-consistency — and it is *weakest* on the
> emotional, cutting-edge material this book lives on. It surfaces where readers get
> bored / confused / split; it does **not** forecast sales or reception, and it
> **never gates, locks, or advances** a chapter. It complements — never replaces —
> the real ARC reader program (Team ⑥ `arc-manager`). See `AGENTS.md §5`.

## How it works

```
corpus      web-search the niche's comp titles (Harari, Bostrom, Chalmers, …)
            → paraphrased Resonance/Complaint/Emotion themes  (research/raw/panel-corpus/)
            + reuse calib-reader-voice / calib-audience artifacts if present
   │
personas    LLM mines the corpus → N grounded archetypes with a SIGNATURE complaint,
            weighted to the readership, variance preserved  (research/panel/roster/)
   │
run         round 1: every persona reads the chapter PRIVATELY (chapter cached as a
                     shared prefix; verbalized sampling + anti-sycophancy controls)
            round 2: peer feed → bounded-confidence re-react   (--rounds 2)
            aggregate VARIANCE-FIRST → ranked objections        (research/panel/reports/panel-<scope>.md)
```

The chapter text is sent once and **prompt-cached**, so persona 2…N read it at ~0.1×
cost. Reactions run on `claude-opus-4-8` (fidelity); aggregation on `claude-sonnet-4-6`.

## Install

```bash
cd reader-panel
uv venv --python 3.12          # isolated; avoids bleeding-edge wheel gaps
uv pip install -e .            # installs the `anthropic` SDK
export ANTHROPIC_API_KEY=sk-ant-...   # required for real runs
```

## Use (the author triggers this manually)

```bash
reader-panel corpus                      # one-time: gather the review-theme corpus
reader-panel personas                    # one-time: freeze the weighted roster (commit it)
reader-panel run --scope CH-00           # fly the introduction through the panel
reader-panel run --scope Part-I --rounds 2 --peer-feed mixed
reader-panel run --scope full
```

Every command accepts `--mock` for an **offline dry run** with no API key — it
exercises the whole pipeline on deterministic canned data so you can see the shape
of the output before spending a cent:

```bash
reader-panel run --scope CH-00 --mock
```

Flags: `--panel-size` (default 30), `--samples` (verbalized reactions/persona),
`--rounds {1,2}`, `--peer-feed {full,echo,mixed}`, `--concurrency`, `--variance-floor`.

## Outputs

- `research/panel/reports/panel-<scope>.md` — the human-readable report (headline = ranked
  objections; distribution + polarization + the harshest minority verbatim).
- `research/panel/runs/<scope>-<stamp>/` — full JSON (round1, round2, report).
- `research/panel/roster/` + `research/panel/roster.json` — the frozen panel.
- `research/raw/panel-corpus/` — provenance-logged review-theme corpus.

## Guardrails baked in

- **Variance-first.** Reports the distribution and the harshest minority, not just a
  mean. Low variance is flagged as a possible **sycophancy** artifact, not a win.
- **Confusing vs challenging.** Objections are tagged FIX (a clarity defect) or
  PROTECT (a deliberate edge) so panel feedback can't sand the book's edges off.
- **No scraping.** Themes only, paraphrased; no verbatim reviews, names, or PII.
- **No gate.** No file in `bible/`, `drafts/`, or `outline/` is ever written; no
  `status`/lock is ever changed; nothing is published.

## Validate before you trust it

Run the validation protocol: collect 10–30 real reader reactions to
a passage (via `arc-manager`), have the panel predict them *before* seeing them, and
keep the panel only on question types where it beats a coin flip on **direction**.

## Tests

```bash
uv run --python 3.12 --with pytest pytest -q   # offline; no API key needed
```
