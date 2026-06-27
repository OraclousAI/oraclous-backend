"""Plain dataclasses for the panel's data, with JSON (de)serialization.

No pydantic / no third-party schema lib — keeps the dependency surface to just
`anthropic` + stdlib, which matters on bleeding-edge Python.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Persona:
    """A data-grounded reader archetype. First-person backstory drives fidelity."""

    persona_id: str
    archetype_name: str
    panel_weight: float                 # cluster share; the roster sums to ~1.0
    provenance: str                     # which corpus/cluster + license note
    backstory: str                      # FIRST PERSON, grounded, not a demographic stub
    reading_habits: str
    reading_level: str
    jtbd: str                           # job-to-be-done
    prior_beliefs: str
    value_prior: str
    resonance: str                      # what delights this archetype
    complaint: str                      # signature complaint (distinct per persona)
    annoyances: list[str] = field(default_factory=list)
    voice_sample: str = ""
    devils_advocate: bool = False
    temperature_note: str = ""          # per-agent diversity note (documentation only)

    def as_prompt_block(self) -> str:
        annoy = ", ".join(self.annoyances) if self.annoyances else "(none noted)"
        return (
            f"You are {self.archetype_name} (id: {self.persona_id}).\n"
            f"Backstory: {self.backstory}\n"
            f"Reading habits: {self.reading_habits}\n"
            f"Reading level: {self.reading_level}\n"
            f"What you want from a book (job-to-be-done): {self.jtbd}\n"
            f"Prior beliefs: {self.prior_beliefs}\n"
            f"What you value most: {self.value_prior}\n"
            f"What delights you: {self.resonance}\n"
            f"Your signature complaint about books like this: {self.complaint}\n"
            f"Things that annoy you: {annoy}\n"
            f"How you talk: {self.voice_sample}\n"
            + ("You read like a contrarian; you push back hard.\n" if self.devils_advocate else "")
        )


@dataclass
class SubReaction:
    """One verbalized-sampling reaction with its probability mass."""

    probability: float
    gut_reaction: str
    pulled_in_at: str
    put_down_at: str
    delighted_by: str
    annoyed_by: str
    strongest_objection: str
    star_rating: int
    one_paragraph_review: str
    would_recommend_to: str
    emotion_felt: str


@dataclass
class PersonaReaction:
    """A persona's full round-1 (or round-2) reaction = a distribution of sub-reactions."""

    persona_id: str
    archetype_name: str
    panel_weight: float
    reactions: list[SubReaction]
    round: int = 1
    did_shift: bool = False
    shifted_because: str = ""
    moved_toward: str = "none"          # more_positive | more_negative | none

    def primary(self) -> SubReaction:
        return max(self.reactions, key=lambda r: r.probability)


@dataclass
class PanelReport:
    scope: str
    timestamp: str
    config: dict[str, Any]
    personas_used: int
    rounds_run: int
    # deterministic stats (computed in Python, not by an LLM)
    star_mean: float
    star_std: float
    star_distribution: dict[str, float]   # "1".."5" -> weighted share
    share_low: float                      # 1-2 stars
    share_high: float                     # 4-5 stars
    polarization_index: float             # 0..1, high => split room
    recommend_rate: float
    variance_flag: str                    # "" or a sycophancy warning
    harshest_minority: dict[str, Any]     # lowest-rated reaction (model-written), probability-gated
    network_movement: dict[str, Any]      # who moved (round 2)
    # LLM synthesis (the headline deliverable)
    ranked_objections: list[dict[str, Any]]
    what_resonated: list[str]
    summary: str


# --- JSON helpers ------------------------------------------------------------

def to_json(obj: Any) -> str:
    def _default(o: Any) -> Any:
        if hasattr(o, "__dataclass_fields__"):
            return asdict(o)
        raise TypeError(f"not serializable: {type(o)}")
    return json.dumps(obj, indent=2, default=_default, ensure_ascii=False)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(obj), encoding="utf-8")


_PERSONA_DEFAULTS: dict[str, Any] = {
    "persona_id": "unknown", "archetype_name": "Reader", "panel_weight": 0.0,
    "provenance": "", "backstory": "", "reading_habits": "", "reading_level": "",
    "jtbd": "", "prior_beliefs": "", "value_prior": "", "resonance": "", "complaint": "",
    "annoyances": [], "voice_sample": "", "devils_advocate": False, "temperature_note": "",
}


def persona_from_dict(d: dict[str, Any]) -> Persona:
    # tolerant of partial dicts (e.g. a roster missing an optional field) — fill
    # any missing/None field with a benign default rather than crashing.
    kwargs = {}
    for field_name in Persona.__dataclass_fields__:
        val = d.get(field_name)
        kwargs[field_name] = _PERSONA_DEFAULTS.get(field_name) if val is None else val
    return Persona(**kwargs)


def load_roster(path: Path) -> list[Persona]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [persona_from_dict(p) for p in data["personas"]]
