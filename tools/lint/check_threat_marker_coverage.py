"""Guardrail: every declared threat marker maps to >= 1 collected test (WP-8, A7).

`pytest.ini` declares threat-mapped markers (the T1-T7 Structured Threat Catalogue
mapping). A marker that maps to ZERO tests is a false coverage signal: `pytest -m
<marker>` collects nothing and exits 5, which a permissive CI step treats as green
— so a threat looks covered when nothing exercises it.

This linter closes that gap permanently:

  1. Parse the marker names declared in ``pytest.ini`` (the ``markers =`` block).
  2. Load the classification manifest ``tools/lint/threat_marker_status.yaml``:
       - ``required`` — must collect >= 1 test;
       - ``deferred`` — intentionally untested (carries a one-line reason), exempt;
       - ``ignore``  — non-threat operational/test-shape markers, not policed.
  3. TMC001 — every declared marker MUST be classified in exactly one bucket (a new
     marker can never silently escape the contract).
  4. TMC002 — a marker classified in the manifest that is NOT declared in pytest.ini
     (stale manifest entry).
  5. TMC003 — for every ``required`` marker, ``pytest --collect-only -m <marker>``
     MUST yield >= 1 collected test. Zero-collection is a hard failure.

Run:  uv run python -m tools.lint.check_threat_marker_coverage
Exits non-zero (1) on any violation; 0 otherwise.
"""

from __future__ import annotations

import configparser
import re
import subprocess
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYTEST_INI = _REPO_ROOT / "pytest.ini"
_MANIFEST = Path(__file__).resolve().parent / "threat_marker_status.yaml"

# A `pytest --collect-only` summary line. With a `-m` selection pytest prints the
# "X/Y tests collected (Z deselected)" form where X is the SELECTED count — capture X,
# not the Y total. Without deselection it prints "X tests collected" / "1 test collected".
_SELECTED_RE = re.compile(r"(\d+)\s*/\s*\d+\s+tests?\s+collected", re.IGNORECASE)
_COLLECTED_RE = re.compile(r"(?<![\d/])(\d+)\s+tests?\s+collected", re.IGNORECASE)


def declared_markers(pytest_ini: Path) -> list[str]:
    """Marker names from the ``markers =`` block of pytest.ini (the part before ``:``)."""
    parser = configparser.ConfigParser()
    parser.read(pytest_ini, encoding="utf-8")
    raw = parser.get("pytest", "markers", fallback="")
    names: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        names.append(line.split(":", 1)[0].strip())
    return names


def load_manifest(manifest: Path) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Return (required{name->reason}, deferred{name->reason}, ignore{names})."""
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    required = dict(data.get("required") or {})
    deferred = dict(data.get("deferred") or {})
    ignore = set(data.get("ignore") or [])
    return required, deferred, ignore


def collected_count(marker: str) -> int:
    """Number of tests `pytest --collect-only -m <marker>` collects (0 on exit-5/no-collect)."""
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            marker,
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # Exit 5 == "no tests collected" — unambiguously zero.
    if proc.returncode == 5:
        return 0
    out = proc.stdout + proc.stderr
    selected = _SELECTED_RE.search(out)
    if selected:
        return int(selected.group(1))
    match = _COLLECTED_RE.search(out)
    if match:
        return int(match.group(1))
    # Fall back to counting node ids (lines containing "::") if the summary is absent.
    return sum(1 for line in proc.stdout.splitlines() if "::" in line)


def check() -> list[str]:
    violations: list[str] = []
    declared = declared_markers(_PYTEST_INI)
    required, deferred, ignore = load_manifest(_MANIFEST)
    classified = set(required) | set(deferred) | ignore

    # TMC001 — every declared marker is classified.
    for name in declared:
        if name not in classified:
            violations.append(
                f"TMC001 {name}: declared in pytest.ini but not classified in "
                f"{_MANIFEST.name} (add it to required/deferred/ignore)."
            )

    # TMC002 — no stale manifest entry (required/deferred name not declared in pytest.ini).
    declared_set = set(declared)
    for name in (*required, *deferred):
        if name not in declared_set:
            violations.append(
                f"TMC002 {name}: classified in {_MANIFEST.name} but not declared in "
                f"pytest.ini (stale manifest entry)."
            )

    # TMC003 — every required marker collects >= 1 test.
    for name in sorted(required):
        if name not in declared_set:
            continue  # already reported as TMC002
        count = collected_count(name)
        if count < 1:
            violations.append(
                f"TMC003 {name}: required threat marker collects 0 tests — "
                f"`pytest -m {name}` is a false coverage signal. Mark a test that "
                f"genuinely exercises this threat, or move it to `deferred` with a reason."
            )
    return violations


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    violations = check()
    for v in violations:
        print(v)
    if violations:
        print(f"\n{len(violations)} threat-marker-coverage violation(s) found.")
        return 1
    print("threat-marker coverage OK: every required marker maps to >= 1 collected test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
