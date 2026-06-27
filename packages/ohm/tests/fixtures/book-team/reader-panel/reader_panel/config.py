"""Run configuration, model IDs, and path layout for the synthetic reader panel.

This is a DIRECTIONAL instrument, not a predictor. See AGENTS.md §5 and the
reader-panel SKILL/agent docs. Nothing here gates, locks, or publishes anything.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --- Models (cloud Claude; the author chose fidelity over local) -------------
# Persona reactions and persona mining are the #1 fidelity drivers -> Opus.
# Web gathering and final aggregation are extraction/clustering -> Sonnet.
REACT_MODEL = "claude-opus-4-8"
MINE_MODEL = "claude-opus-4-8"
NETWORK_MODEL = "claude-opus-4-8"
GATHER_MODEL = "claude-sonnet-4-6"
AGG_MODEL = "claude-sonnet-4-6"

# --- Comp titles for this book's niche (big-idea consciousness/AI nonfiction) --
# Used to seed the review corpus. Edit freely; provenance is logged per run.
COMP_TITLES: list[tuple[str, str]] = [
    ("Sapiens", "Yuval Noah Harari"),
    ("Homo Deus", "Yuval Noah Harari"),
    ("Nexus", "Yuval Noah Harari"),
    ("The Singularity Is Near", "Ray Kurzweil"),
    ("The Singularity Is Nearer", "Ray Kurzweil"),
    ("Superintelligence", "Nick Bostrom"),
    ("Life 3.0", "Max Tegmark"),
    ("The Conscious Mind", "David Chalmers"),
    ("Being You", "Anil Seth"),
    ("Conscious", "Annaka Harris"),
    ("A Brief History of Intelligence", "Max Bennett"),
    ("The Coming Wave", "Mustafa Suleyman"),
]

# --- Paths (relative to the studio root, resolved at runtime) -----------------
STUDIO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Paths:
    # default_factory reads the module global at call time, so STUDIO_ROOT stays
    # patchable (tests redirect it to a temp dir).
    root: Path = field(default_factory=lambda: STUDIO_ROOT)

    @property
    def corpus_dir(self) -> Path:
        # research/raw/ is append-only ground evidence per AGENTS.md.
        return self.root / "research" / "raw" / "panel-corpus"

    @property
    def roster_dir(self) -> Path:
        return self.root / "research" / "panel" / "roster"

    @property
    def runs_dir(self) -> Path:
        return self.root / "research" / "panel" / "runs"

    @property
    def reports_dir(self) -> Path:
        # Team ① advisory area (qa/ is Team ④'s per AGENTS.md §3). The report uses
        # the engagement-reviewer's vocabulary so it stays commensurable.
        return self.root / "research" / "panel" / "reports"

    @property
    def calibration_dir(self) -> Path:
        # Reuse calib-reader-voice / calib-audience outputs when present.
        return self.root / "research" / "calibration"

    @property
    def drafts_dir(self) -> Path:
        return self.root / "drafts"

    def ensure(self) -> None:
        for d in (self.corpus_dir, self.roster_dir, self.runs_dir, self.reports_dir):
            d.mkdir(parents=True, exist_ok=True)


@dataclass
class RunConfig:
    """Knobs for one panel run. Frozen-by-default values keep results comparable."""

    panel_size: int = 30                 # grounded personas; dozens, not thousands
    samples_per_persona: int = 3         # verbalized-sampling reactions per persona
    rounds: int = 1                      # 1 = private read only; 2 = + word-of-mouth
    peer_feed_mode: str = "mixed"        # full | echo | mixed (round 2 only)
    max_concurrency: int = 6             # API fan-out cap
    variance_floor: float = 0.5          # std below this => sycophancy red flag
    mock: bool = False                   # offline deterministic mode (no API key)
    api_key_env: str = "ANTHROPIC_API_KEY"

    def resolve_credential(self) -> str | None:
        """Return an explicit credential if present. The SDK also resolves an
        `ant auth login` profile, so a missing env var is not necessarily fatal."""
        if self.mock:
            return None
        return os.environ.get(self.api_key_env) or os.environ.get("ANTHROPIC_AUTH_TOKEN")


# The emotional target_state vocabulary (rules/emotional-architecture.md). The
# panel reports felt-emotion against these so output is commensurable with the
# engagement-reviewer.
TARGET_STATES = [
    "recognition/dopamine-prime",
    "curiosity/dopamine",
    "clarity/serotonin+oxytocin",
    "urgency/norepinephrine",
    "awe/dopamine",
    "agency/serotonin",
]
