"""Personas stage: mine the corpus into archetypes, weight them to the readership
(preserving within-segment variance), and freeze a version-controlled roster.

The roster is the panel's calibration. Freeze it (git) before scoring chapters so
runs stay comparable; re-mine only when the corpus or target readership changes.
"""
from __future__ import annotations

import re

from . import config
from .llm import Client
from .models import Persona, persona_from_dict, write_json


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def build_roster(client: Client, cfg: config.RunConfig, corpus_text: str) -> list[Persona]:
    raw = client.mine_archetypes(corpus_text, cfg.panel_size)
    personas: list[Persona] = []
    seen_ids: set[str] = set()
    for i, d in enumerate(raw):
        d.setdefault("annoyances", [])
        d.setdefault("voice_sample", "")
        d.setdefault("devils_advocate", False)
        pid = (d.get("persona_id") or _slug(d.get("archetype_name", f"reader-{i}")))
        # guarantee uniqueness — IDs are permanent identifiers (AGENTS.md §6)
        base, n = pid, 2
        while pid in seen_ids:
            pid = f"{base}-{n:02d}"
            n += 1
        seen_ids.add(pid)
        d["persona_id"] = pid
        personas.append(persona_from_dict(d))

    _normalize_weights(personas)
    _write(personas)
    return personas


def _normalize_weights(personas: list[Persona]) -> None:
    total = sum(max(0.0, p.panel_weight) for p in personas) or 1.0
    for p in personas:
        p.panel_weight = round(max(0.0, p.panel_weight) / total, 4)


def _write(personas: list[Persona]) -> None:
    paths = config.Paths()
    paths.roster_dir.mkdir(parents=True, exist_ok=True)
    # machine-readable roster (the source of truth for runs)
    write_json(paths.roster_dir.parent / "roster.json", {"personas": personas})
    # human-readable cards
    for p in personas:
        card = _render_card(p)
        (paths.roster_dir / f"{p.persona_id}.md").write_text(card, encoding="utf-8")


def _render_card(p: Persona) -> str:
    annoy = "\n".join(f"  - {a}" for a in p.annoyances) or "  - (none)"
    return (
        f"# {p.archetype_name}\n\n"
        f"- **persona_id:** {p.persona_id}\n"
        f"- **panel_weight:** {p.panel_weight}\n"
        f"- **devils_advocate:** {p.devils_advocate}\n"
        f"- **provenance:** {p.provenance}\n\n"
        f"**Backstory (first person):** {p.backstory}\n\n"
        f"**Reading habits:** {p.reading_habits}\n\n"
        f"**Reading level:** {p.reading_level}\n\n"
        f"**Job-to-be-done:** {p.jtbd}\n\n"
        f"**Prior beliefs:** {p.prior_beliefs}\n\n"
        f"**Value prior:** {p.value_prior}\n\n"
        f"**What delights them (resonance):** {p.resonance}\n\n"
        f"**Signature complaint:** {p.complaint}\n\n"
        f"**Annoyances:**\n{annoy}\n\n"
        f"**Voice sample:** {p.voice_sample}\n"
    )
