"""Aggregation + report. Variance-FIRST: we report the distribution, the
polarization, and the harshest minority reaction — never just an average. Low
variance is treated as a sycophancy red flag, not a success. Everything is
labeled SYNTHETIC and never gates anything."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .llm import Client
from .models import PanelReport, PersonaReaction, write_json


def _weighted_distribution(reactions: list[PersonaReaction]) -> dict[int, float]:
    dist = {s: 0.0 for s in range(1, 6)}
    for r in reactions:
        for sub in r.reactions:
            star = min(5, max(1, int(sub.star_rating)))
            dist[star] += r.panel_weight * sub.probability
    total = sum(dist.values()) or 1.0
    return {s: v / total for s, v in dist.items()}


def _stats(reactions: list[PersonaReaction]) -> dict:
    dist = _weighted_distribution(reactions)
    mean = sum(s * w for s, w in dist.items())
    var = sum(w * (s - mean) ** 2 for s, w in dist.items())
    std = math.sqrt(var)
    share_low = dist[1] + dist[2]
    share_high = dist[4] + dist[5]
    polarization = round(2 * min(share_low, share_high), 3)
    return dict(dist={str(k): round(v, 3) for k, v in dist.items()},
                mean=round(mean, 2), std=round(std, 2),
                share_low=round(share_low, 3), share_high=round(share_high, 3),
                polarization=polarization)


_NO_REC = ("no one", "no-one", "noone", "nobody", "none", "not for",
           "would not", "wouldn't", "won't", "not recommend", "do not recommend",
           "dnf", "skip it")


def _recommend_rate(reactions: list[PersonaReaction]) -> float:
    pos = tot = 0.0
    for r in reactions:
        for sub in r.reactions:
            w = r.panel_weight * sub.probability
            tot += w
            rec = (sub.would_recommend_to or "").strip().lower()
            # free-text field — treat empties and explicit negations as "no"
            # (exact-match let "no one yet" / "probably nobody" inflate the rate)
            if rec and not any(p in rec for p in _NO_REC):
                pos += w
    return round(pos / (tot or 1.0), 3)


def _harshest_minority(reactions: list[PersonaReaction], prob_floor: float = 0.15) -> dict:
    # only consider sub-reactions carrying real probability mass, so a near-zero
    # verbalized-sampling tail can't headline the report. Fall back to all if none clear it.
    worst = None
    for floor in (prob_floor, 0.0):
        for r in reactions:
            for sub in r.reactions:
                if sub.probability < floor:
                    continue
                if worst is None or sub.star_rating < worst[1].star_rating:
                    worst = (r, sub)
        if worst is not None:
            break
    if not worst:
        return {}
    r, sub = worst
    return dict(archetype=r.archetype_name, persona_id=r.persona_id,
                star=sub.star_rating, review=sub.one_paragraph_review,
                strongest_objection=sub.strongest_objection)


def _network_movement(round1, round2) -> dict:
    if not round2:
        return {}
    shifted = [r for r in round2 if r.did_shift]
    s1 = _stats(round1)
    s2 = _stats(round2)
    return dict(
        moved=len(shifted),
        of=len(round2),
        directions={
            "more_negative": sum(1 for r in shifted if r.moved_toward == "more_negative"),
            "more_positive": sum(1 for r in shifted if r.moved_toward == "more_positive"),
        },
        polarization_before=s1["polarization"],
        polarization_after=s2["polarization"],
        mean_before=s1["mean"],
        mean_after=s2["mean"],
        movers=[dict(archetype=r.archetype_name, toward=r.moved_toward,
                     because=r.shifted_because) for r in shifted][:12],
    )


def _digest(reactions: list[PersonaReaction]) -> str:
    rows = []
    for r in sorted(reactions, key=lambda x: x.panel_weight, reverse=True):
        pr = r.primary()
        rows.append(
            f"- [{r.panel_weight:.2f}] {r.archetype_name}: {pr.star_rating}★ | "
            f"objection: {pr.strongest_objection} | review: {pr.one_paragraph_review}"
        )
    return "\n".join(rows)


def build(client: Client, cfg: config.RunConfig, scope: str,
          round1: list[PersonaReaction], round2: list[PersonaReaction] | None) -> PanelReport:
    final = round2 or round1
    synth = client.synthesize(scope, _digest(final))
    return build_from_reactions(scope, round1, round2, synth,
                                variance_floor=cfg.variance_floor,
                                react_model=config.REACT_MODEL,
                                samples_per_persona=cfg.samples_per_persona,
                                peer_feed_mode=cfg.peer_feed_mode, mock=cfg.mock)


def build_from_reactions(scope: str, round1: list[PersonaReaction],
                         round2: list[PersonaReaction] | None, synthesis: dict, *,
                         variance_floor: float = 0.5, react_model: str = config.REACT_MODEL,
                         samples_per_persona=None, peer_feed_mode: str = "mixed",
                         mock: bool = False) -> PanelReport:
    """Assemble a report from already-collected reactions + synthesis. Used both by
    the API pipeline (build) and the Max/Claude-Code ingest path (cli ingest)."""
    final = round2 or round1
    st = _stats(final)
    flag = ""
    if st["std"] < variance_floor:
        flag = (f"LOW VARIANCE (std={st['std']} < {variance_floor}): the panel is "
                "suspiciously unanimous. Treat as a possible sycophancy artifact, not a win.")
    return PanelReport(
        scope=scope,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        config=dict(panel_size=len(final), samples_per_persona=samples_per_persona,
                    peer_feed_mode=peer_feed_mode, react_model=react_model, mock=mock),
        personas_used=len(final), rounds_run=(2 if round2 else 1),
        star_mean=st["mean"], star_std=st["std"], star_distribution=st["dist"],
        share_low=st["share_low"], share_high=st["share_high"],
        polarization_index=st["polarization"], recommend_rate=_recommend_rate(final),
        variance_flag=flag, harshest_minority=_harshest_minority(final),
        network_movement=_network_movement(round1, round2),
        ranked_objections=synthesis.get("ranked_objections", []),
        what_resonated=synthesis.get("what_resonated", []),
        summary=synthesis.get("summary", "(synthesis unavailable — check stderr for a skipped call)"),
    )


def render(rep: PanelReport, run_dir: Path,
           round1: list[PersonaReaction], round2) -> Path:
    paths = config.Paths()
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "report.json", rep)
    write_json(run_dir / "round1.json", {"reactions": round1})
    if round2:
        write_json(run_dir / "round2.json", {"reactions": round2})

    bars = "\n".join(
        f"  {s}★  {'█' * round(rep.star_distribution.get(str(s), 0) * 40):<40} "
        f"{rep.star_distribution.get(str(s), 0) * 100:.0f}%"
        for s in range(5, 0, -1)
    )
    obj_lines = []
    for i, o in enumerate(rep.ranked_objections, 1):
        obj_lines.append(
            f"{i}. **[{o['severity'].upper()} · {o['tag']} → {o['fix_or_protect']}]** "
            f"{o['objection']}\n"
            f"   - segments: {', '.join(o['segments_affected'])}; {o['frequency_signal']}\n"
            f"   - representative: _{o['representative_quote']}_"
        )
    net = rep.network_movement
    net_md = ""
    if net:
        movers = "\n".join(f"  - {m['archetype']} → {m['toward']}: {m['because']}"
                           for m in net.get("movers", [])) or "  - (none moved)"
        net_md = (
            f"\n## Word-of-mouth (round 2)\n"
            f"- moved: **{net['moved']}/{net['of']}** "
            f"({net['directions']['more_negative']}↓ / {net['directions']['more_positive']}↑)\n"
            f"- mean {net['mean_before']} → {net['mean_after']}; "
            f"polarization {net['polarization_before']} → {net['polarization_after']}\n"
            f"{movers}\n"
        )
    hm = rep.harshest_minority
    hm_md = (
        f"- **{hm['archetype']}** ({hm['star']}★): _{hm['review']}_\n"
        f"  - objection: {hm['strongest_objection']}\n" if hm else "- (none)\n"
    )

    md = f"""# Synthetic Reader Panel — {rep.scope}

> **⚠️ SYNTHETIC — directional signal only.** Generated by LLM reader agents grounded
> in real review data. This is a wind tunnel, NOT a predictor and NOT a verdict. It
> does not gate, lock, or advance anything (AGENTS.md §5: no part locks on synthetic
> signal alone; ≥ 5 real ARC/beta reactions required). Pair with the real ARC program.

- **run:** {rep.timestamp} · personas: {rep.personas_used} · rounds: {rep.rounds_run} · mock: {rep.config.get('mock')}
- **model:** {rep.config.get('react_model')}

## Headline — ranked objections (the deliverable)
{chr(10).join(obj_lines) if obj_lines else '- (none)'}

## What resonated
{chr(10).join('- ' + w for w in rep.what_resonated) or '- (none)'}

## Distribution (variance-first; an average alone would lie)
- mean **{rep.star_mean}★** · std **{rep.star_std}** · polarization **{rep.polarization_index}** (0=consensus, 1=split room)
- low (1–2★): {rep.share_low*100:.0f}% · high (4–5★): {rep.share_high*100:.0f}% · would-recommend: {rep.recommend_rate*100:.0f}%
```
{bars}
```
{('> ' + rep.variance_flag) if rep.variance_flag else ''}

## Lowest-rated reaction (model-written; read this one)
{hm_md}
{net_md}
## Synthesis
{rep.summary}

---
_Reproduce: roster under `research/panel/roster/`, full run under `{run_dir}`._
"""
    report_path = paths.reports_dir / f"panel-{rep.scope}.md"
    report_path.write_text(md, encoding="utf-8")
    return report_path
