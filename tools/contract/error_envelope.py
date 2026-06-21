"""Reference loader, scanner, and checksum logic for the gateway error-envelope
contract fixture (Interface Contracts §3).

The DATA under ``packages/errors/contract/`` is the single source of truth shared
across repositories (copied-with-checksum per the Cross-cutting agreement protocol
§2.6). This module is the backend's reference consumer; the frontend api-client
mirrors the equivalent in TypeScript against the same JSON files.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = REPO_ROOT / "packages" / "errors" / "contract"
SCHEMA_PATH = CONTRACT_DIR / "error-envelope.schema.json"
TAXONOMY_PATH = CONTRACT_DIR / "error-code-taxonomy.json"
FORBIDDEN_PATH = CONTRACT_DIR / "forbidden-substrings.json"
SAMPLES_DIR = CONTRACT_DIR / "samples"
CHECKSUMS_PATH = CONTRACT_DIR / "CHECKSUMS.sha256"

_FLAG_MAP: dict[str, re.RegexFlag] = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_schema() -> dict[str, Any]:
    return _load_json(SCHEMA_PATH)


def load_taxonomy() -> dict[str, Any]:
    return _load_json(TAXONOMY_PATH)


def iter_sample_paths() -> list[Path]:
    return sorted(SAMPLES_DIR.glob("*.json"))


def load_samples() -> dict[str, Any]:
    """Map of code -> parsed sample body, keyed by sample filename stem."""
    return {p.stem: _load_json(p) for p in iter_sample_paths()}


class ForbiddenPattern:
    """A single sensitive-data leak pattern from forbidden-substrings.json."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.id: str = spec["id"]
        self.rule: int = spec["rule"]
        self.description: str = spec["description"]
        self.example: str = spec["example"]
        flags: re.RegexFlag = re.NOFLAG
        for ch in spec.get("flags", ""):
            flags |= _FLAG_MAP[ch]
        self.regex: re.Pattern[str] = re.compile(spec["regex"], flags)


def load_forbidden_patterns() -> list[ForbiddenPattern]:
    data = _load_json(FORBIDDEN_PATH)
    return [ForbiddenPattern(p) for p in data["patterns"]]


def scan_forbidden(text: str, patterns: list[ForbiddenPattern] | None = None) -> list[str]:
    """Return the ids of every forbidden pattern that matches ``text``."""
    pats = patterns if patterns is not None else load_forbidden_patterns()
    return [p.id for p in pats if p.regex.search(text)]


# --- checksums -------------------------------------------------------------


def _artifact_paths() -> list[Path]:
    return sorted([SCHEMA_PATH, TAXONOMY_PATH, FORBIDDEN_PATH, *iter_sample_paths()])


def compute_checksums() -> dict[str, str]:
    """sha256 of each artifact, keyed by its path relative to CONTRACT_DIR (posix)."""
    out: dict[str, str] = {}
    for path in _artifact_paths():
        rel = path.relative_to(CONTRACT_DIR).as_posix()
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def render_checksums() -> str:
    lines = [f"{digest}  {rel}" for rel, digest in sorted(compute_checksums().items())]
    return "\n".join(lines) + "\n"


def parse_checksums(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, rel = line.partition("  ")
        out[rel.strip()] = digest.strip()
    return out


def verify_checksums() -> list[str]:
    """Return human-readable drift errors; an empty list means the manifest matches."""
    if not CHECKSUMS_PATH.exists():
        return [f"missing checksum manifest: {CHECKSUMS_PATH}"]
    recorded = parse_checksums(CHECKSUMS_PATH.read_text(encoding="utf-8"))
    actual = compute_checksums()
    errors: list[str] = []
    for rel in sorted(set(recorded) | set(actual)):
        if rel not in actual:
            errors.append(f"recorded artifact missing on disk: {rel}")
        elif rel not in recorded:
            errors.append(f"artifact not recorded in manifest: {rel}")
        elif recorded[rel] != actual[rel]:
            errors.append(f"checksum drift: {rel}")
    return errors
