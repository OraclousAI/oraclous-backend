"""CLI for the synthetic reader panel.

    reader-panel corpus                 # gather review-theme corpus (web search)
    reader-panel personas               # mine the corpus -> weighted roster
    reader-panel run --scope CH-00      # fly a chapter through the panel
    reader-panel run --scope full --rounds 2 --peer-feed mixed

Add --mock to any command for an offline dry run (no API key needed).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import config, corpus, engine, personas, report
from .llm import Client
from .models import load_roster, persona_from_dict


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mock", action="store_true", help="offline deterministic mode (no API key)")
    p.add_argument("--panel-size", type=int, default=config.RunConfig.panel_size)
    p.add_argument("--samples", type=int, default=config.RunConfig.samples_per_persona,
                   help="verbalized-sampling reactions per persona")
    p.add_argument("--concurrency", type=int, default=config.RunConfig.max_concurrency)
    p.add_argument("--variance-floor", type=float, default=config.RunConfig.variance_floor)


def _cfg(args) -> config.RunConfig:
    return config.RunConfig(
        panel_size=args.panel_size, samples_per_persona=args.samples,
        rounds=getattr(args, "rounds", 1), peer_feed_mode=getattr(args, "peer_feed", "mixed"),
        max_concurrency=args.concurrency, variance_floor=args.variance_floor, mock=args.mock,
    )


def cmd_corpus(args) -> None:
    cfg = _cfg(args)
    client = Client(cfg)
    text = corpus.gather(client, cfg)
    print(f"corpus written to {config.Paths().corpus_dir} ({len(text):,} chars)")


def cmd_personas(args) -> None:
    cfg = _cfg(args)
    client = Client(cfg)
    text = corpus.load_existing_corpus()
    if not text:
        print("no corpus on disk; gathering first…")
        text = corpus.gather(client, cfg)
    roster = personas.build_roster(client, cfg, text)
    print(f"built {len(roster)} personas -> {config.Paths().roster_dir}")
    for p in roster:
        print(f"  [{p.panel_weight:>5.2f}] {p.persona_id}  ({p.archetype_name})")


def cmd_run(args) -> None:
    cfg = _cfg(args)
    client = Client(cfg)
    paths = config.Paths()
    paths.ensure()

    roster_path = paths.roster_dir.parent / "roster.json"
    if not roster_path.exists():
        print("no roster found; building one first…")
        text = corpus.load_existing_corpus() or corpus.gather(client, cfg)
        personas.build_roster(client, cfg, text)
    plist = load_roster(roster_path)
    if not plist:
        raise SystemExit("roster is empty; run `reader-panel personas` first")

    label, chapter_text = engine.resolve_scope(args.scope)
    print(f"flying scope '{label}' ({len(chapter_text):,} chars) through {len(plist)} personas…")

    r1 = engine.run_round1(client, cfg, plist, chapter_text)
    r2 = engine.run_round2(client, cfg, plist, chapter_text, r1) if cfg.rounds >= 2 else None

    rep = report.build(client, cfg, label, r1, r2)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = paths.runs_dir / f"{label}-{stamp}"
    path = report.render(rep, run_dir, r1, r2)

    print(f"\n=== {label} (SYNTHETIC) ===")
    print(f"mean {rep.star_mean}★  std {rep.star_std}  polarization {rep.polarization_index}  "
          f"recommend {rep.recommend_rate*100:.0f}%")
    if rep.variance_flag:
        print(f"⚠ {rep.variance_flag}")
    print("top objections:")
    for o in rep.ranked_objections[:3]:
        print(f"  - [{o['severity']}/{o['tag']}→{o['fix_or_protect']}] {o['objection']}")
    print(f"\nreport: {path}\nrun:    {run_dir}")


def cmd_ingest(args) -> None:
    """Ingest a panel run produced on the Max plan via Claude Code subagents
    (a workflow result JSON: {personas, round1, round2, synthesis}) into the same
    variance-first report as the API pipeline. No API key needed."""
    data = json.loads(Path(args.result).read_text(encoding="utf-8"))
    res = data.get("result", data)  # accept the task-output wrapper or the raw result
    if "round1" not in res:
        raise SystemExit("result JSON has no 'round1'; expected {personas, round1, round2, synthesis}")

    plist = [persona_from_dict(p) for p in res.get("personas", [])]
    if plist:
        personas._normalize_weights(plist)
        personas._write(plist)  # freeze the roster cards + roster.json

    round1 = [engine.reaction_from_dict(d, 1) for d in res.get("round1", [])]
    round2 = [engine.reaction_from_dict(d, 2) for d in res.get("round2", [])] or None
    synth = res.get("synthesis", {})

    rep = report.build_from_reactions(args.scope, round1, round2, synth,
                                      react_model=args.model, peer_feed_mode=args.peer_feed)
    paths = config.Paths()
    paths.ensure()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = paths.runs_dir / f"{args.scope}-{stamp}"
    path = report.render(rep, run_dir, round1, round2)

    print(f"\n=== {args.scope} (SYNTHETIC) ===")
    print(f"mean {rep.star_mean}★  std {rep.star_std}  polarization {rep.polarization_index}  "
          f"recommend {rep.recommend_rate*100:.0f}%")
    if rep.variance_flag:
        print(f"⚠ {rep.variance_flag}")
    for o in rep.ranked_objections[:3]:
        print(f"  - [{o['severity']}/{o['tag']}→{o['fix_or_protect']}] {o['objection']}")
    print(f"\nreport: {path}\nrun:    {run_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reader-panel",
                                     description="Synthetic reader panel (directional, not a predictor).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("corpus", help="gather review-theme corpus via web search")
    _add_common(pc)
    pc.set_defaults(func=cmd_corpus)

    pp = sub.add_parser("personas", help="mine corpus into a weighted roster")
    _add_common(pp)
    pp.set_defaults(func=cmd_personas)

    pr = sub.add_parser("run", help="fly a chapter/part/full book through the panel")
    pr.add_argument("--scope", required=True, help="CH-NN | Part-I | full | path")
    pr.add_argument("--rounds", type=int, default=1, choices=[1, 2],
                    help="1 = private read; 2 = + word-of-mouth")
    pr.add_argument("--peer-feed", default="mixed", choices=["full", "echo", "mixed"])
    _add_common(pr)
    pr.set_defaults(func=cmd_run)

    pi = sub.add_parser("ingest", help="ingest a Max/Claude-Code panel run (JSON) into a report")
    pi.add_argument("--result", required=True, help="path to the workflow result JSON")
    pi.add_argument("--scope", required=True, help="label for the report, e.g. part-i")
    pi.add_argument("--model", default="Max via Claude Code", help="label for provenance")
    pi.add_argument("--peer-feed", default="mixed")
    pi.set_defaults(func=cmd_ingest)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
