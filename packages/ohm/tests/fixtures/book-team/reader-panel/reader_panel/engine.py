"""The reaction engine.

Round 1 — every persona reads the chapter PRIVATELY (reading is private, not a
feed). The chapter is a cache_control'd shared prefix: the first reaction primes
the cache, the rest read it at ~0.1x cost.

Round 2 (optional, the MiroFish word-of-mouth layer) — personas see a peer feed
and re-react under bounded-confidence updating: they move only if a specific peer
genuinely shifted them. Output is a relative signal + a distribution, never a
virality number.
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config
from .llm import Client
from .models import Persona, PersonaReaction, SubReaction

_FRAME = (
    "You are about to read part of a non-fiction book for a curious general reader. "
    "After the chapter you will be told which specific reader you are and asked to "
    "react in character. Here is the text:\n\n=== CHAPTER ===\n"
)


def _strip_anchors(text: str) -> str:
    """Readers don't see bible citation anchors; strip [[...]] tokens."""
    return re.sub(r"\[\[[^\]]+\]\]", "", text)


def resolve_scope(scope: str) -> tuple[str, str]:
    """Return (label, chapter_text) for a chapter id, 'full', a path, or 'Part-X'."""
    paths = config.Paths()
    drafts = paths.drafts_dir

    p = Path(scope)
    if p.exists():
        if p.is_dir():
            text = "\n\n".join(f.read_text(encoding="utf-8") for f in sorted(p.glob("*.md")))
        else:
            text = p.read_text(encoding="utf-8")
        return p.stem, _strip_anchors(text)

    if scope.lower() == "full":
        files = sorted(drafts.glob("CH-*.md")) or sorted(drafts.glob("*.md"))
        text = "\n\n".join(f.read_text(encoding="utf-8") for f in files)
        return "full-manuscript", _strip_anchors(text)

    m = re.match(r"(?i)CH-?(\d+)", scope)
    if m:
        f = drafts / f"CH-{int(m.group(1)):02d}.md"
        if not f.exists():
            raise SystemExit(f"no draft at {f}")
        return f.stem, _strip_anchors(f.read_text(encoding="utf-8"))

    if scope.lower().startswith("part"):
        ids = _part_chapter_ids(scope)
        files = [drafts / f"{cid}.md" for cid in ids]
        files = [f for f in files if f.exists()]
        if not files:
            raise SystemExit(f"no drafted chapters found for scope '{scope}'")
        text = "\n\n".join(f.read_text(encoding="utf-8") for f in files)
        return _slug(scope), _strip_anchors(text)

    raise SystemExit(f"unrecognized scope '{scope}' (use CH-NN, Part-I, full, or a path)")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _part_chapter_ids(scope: str) -> list[str]:
    toc = config.Paths().root / "outline" / "TOC.json"
    if not toc.exists():
        return []
    try:
        data = json.loads(toc.read_text(encoding="utf-8"))
    except Exception:
        return []
    # canonical part token: strip a leading "part-" so "Part-II" -> "ii", then
    # compare for EQUALITY (the old endswith clauses made Part-II match Part-I,
    # since "i" is a suffix of "part-ii").
    want = _slug(scope)
    key = want[len("part-"):] if want.startswith("part-") else want
    out: list[str] = []
    for ch in _iter_chapters(data):
        if _slug(str(ch.get("part", ""))) == key:
            cid = ch.get("id") or ch.get("chapter_id")
            if cid and cid not in out:  # dedup — defensive
                out.append(cid)
    return out


def _iter_chapters(data):
    # Visit each node ONCE. The old version recursed into named keys AND then into
    # all values, yielding every chapter dict once per nesting level (4x on the real
    # TOC). Yield any dict carrying an id, then recurse into each value exactly once.
    if isinstance(data, dict):
        if "id" in data or "chapter_id" in data:
            yield data
        for v in data.values():
            if isinstance(v, (list, dict)):
                yield from _iter_chapters(v)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_chapters(item)


# --- reactions ---------------------------------------------------------------

def _to_reaction(persona: Persona, raw: dict, round_no: int,
                 did_shift=False, because="", toward="none") -> PersonaReaction:
    subs = [SubReaction(**_clean_sub(s)) for s in raw.get("reactions", raw.get("updated", []))]
    if not subs:  # never let an empty reaction through
        subs = [SubReaction(1.0, "(no reaction parsed)", "", "", "", "", "(none)", 3,
                            "(empty)", "no one", "")]
    # normalize probabilities
    tot = sum(max(0.0, s.probability) for s in subs) or 1.0
    for s in subs:
        s.probability = max(0.0, s.probability) / tot
    return PersonaReaction(
        persona_id=persona.persona_id, archetype_name=persona.archetype_name,
        panel_weight=persona.panel_weight, reactions=subs, round=round_no,
        did_shift=did_shift, shifted_because=because, moved_toward=toward,
    )


def _clean_sub(s: dict) -> dict:
    keys = SubReaction.__dataclass_fields__
    out = {k: s.get(k) for k in keys}
    out["probability"] = float(out.get("probability") or 0.0)
    out["star_rating"] = int(out.get("star_rating") or 3)
    for k in keys:
        if out[k] is None:
            out[k] = "" if k != "star_rating" and k != "probability" else out[k]
    return out


def reaction_from_dict(d: dict, round_no: int) -> PersonaReaction:
    """Convert a reaction dict (e.g. from a Max/Claude-Code workflow run) into a
    PersonaReaction, reusing the same cleaning + probability normalization as the
    API path."""
    subs = [SubReaction(**_clean_sub(s)) for s in d.get("reactions", [])]
    if not subs:
        subs = [SubReaction(1.0, "(no reaction parsed)", "", "", "", "", "(none)", 3,
                            "(empty)", "no one", "")]
    tot = sum(max(0.0, s.probability) for s in subs) or 1.0
    for s in subs:
        s.probability = max(0.0, s.probability) / tot
    return PersonaReaction(
        persona_id=d.get("persona_id", "?"), archetype_name=d.get("archetype_name", ""),
        panel_weight=float(d.get("panel_weight", 0.0)), reactions=subs, round=round_no,
        did_shift=bool(d.get("did_shift", False)), shifted_because=d.get("shifted_because", "") or "",
        moved_toward=d.get("moved_toward", "none") or "none",
    )


def run_round1(client: Client, cfg: config.RunConfig, personas: list[Persona],
               chapter_text: str) -> list[PersonaReaction]:
    if not personas:
        return []
    shared = [{"type": "text", "text": _FRAME + chapter_text + "\n=== END CHAPTER ===",
               "cache_control": {"type": "ephemeral"}}]

    def one(p: Persona) -> PersonaReaction:
        try:
            return _to_reaction(p, client.react(shared, p, cfg.samples_per_persona), 1)
        except Exception as e:  # one bad reaction must not abort the whole round
            sys.stderr.write(f"[reader-panel] reaction failed for {p.persona_id}: {e}\n")
            return _to_reaction(p, {}, 1)

    results: list[PersonaReaction | None] = [None] * len(personas)
    # prime the cache with the first persona, then fan out
    results[0] = one(personas[0])
    if len(personas) > 1:
        with ThreadPoolExecutor(max_workers=cfg.max_concurrency) as ex:
            futs = {ex.submit(one, p): i for i, p in enumerate(personas) if i > 0}
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()
    return [r for r in results if r is not None]


def _peer_feed(round1: list[PersonaReaction], mode: str) -> str:
    rows = []
    stars = [r.primary().star_rating for r in round1]
    median = sorted(stars)[len(stars) // 2] if stars else 3
    for r in round1:
        pr = r.primary()
        if mode == "echo" and abs(pr.star_rating - median) > 1:
            continue  # echo chamber: amplify the majority lean
        rows.append(f"- {r.archetype_name}: {pr.star_rating}★ — \"{pr.gut_reaction}\" "
                    f"(objection: {pr.strongest_objection})")
    if mode == "mixed" and len(rows) > 12:
        rows = rows[:6] + rows[-6:]  # balanced sample of high and low
    return "\n".join(rows)


def run_round2(client: Client, cfg: config.RunConfig, personas: list[Persona],
               chapter_text: str, round1: list[PersonaReaction]) -> list[PersonaReaction]:
    if not personas or not round1:
        return []
    feed = _peer_feed(round1, cfg.peer_feed_mode)
    shared = [{
        "type": "text",
        "text": _FRAME + chapter_text + "\n=== END CHAPTER ===\n\n"
                "=== WHAT OTHER READERS SAID ===\n" + feed,
        "cache_control": {"type": "ephemeral"},
    }]
    prior_by_id = {r.persona_id: {"reactions": [s.__dict__ for s in r.reactions]} for r in round1}

    def one(p: Persona) -> PersonaReaction:
        try:
            raw = client.network_react(shared, p, prior_by_id[p.persona_id])
            return _to_reaction(p, raw, 2, raw.get("did_shift", False),
                                raw.get("shifted_because", ""), raw.get("moved_toward", "none"))
        except Exception as e:  # isolate failures; hold the persona's round-1 stance
            sys.stderr.write(f"[reader-panel] round-2 failed for {p.persona_id}: {e}\n")
            prior = prior_by_id[p.persona_id]
            return _to_reaction(p, {"reactions": prior["reactions"]}, 2)

    results: list[PersonaReaction | None] = [None] * len(personas)
    results[0] = one(personas[0])
    if len(personas) > 1:
        with ThreadPoolExecutor(max_workers=cfg.max_concurrency) as ex:
            futs = {ex.submit(one, p): i for i, p in enumerate(personas) if i > 0}
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()
    return [r for r in results if r is not None]
