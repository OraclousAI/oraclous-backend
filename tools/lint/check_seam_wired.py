"""Seam-wiring presence guardrail — a built control seam MUST be referenced on at least one
service request path, so it cannot regress to "built but unwired" unnoticed.

The bug class (#446 / #456): the ReBAC engine (``packages/rebac``) was fully built and unit-tested,
but it was wired into ZERO request paths — every cross-org read fell straight through it. The only
test mocked the access boundary, so a latent engine bug (missing system-Permission seed) AND the
total absence of wiring both stayed invisible behind a green CI. A static "does the policy/engine
exist?" check passes in that state; what was missing is "is it actually CALLED on a request path?".

This guardrail closes the *total-absence* half statically. For each seam in
``tools/lint/seam_wiring.yaml`` it scans every service ``src/`` (tests excluded) and requires the
seam's ``symbol`` to appear in at least one service. A seam whose symbol appears in NO service src
is flagged (SEAM001) — exactly the pre-#446 state, where ``authorise_cross_org_traversal`` was
defined in the substrate package but called from nowhere.

KNOWN LIMITATION (by design, mirrors ``check_rls_request_binding``): this is a PRESENCE check. It
proves the wiring symbol exists *somewhere* on a service path; it does NOT prove the call is on the
RIGHT path, with the RIGHT arguments, reaching the RIGHT decision. That is the job of the
per-feature DEPLOYED-STACK e2e (the #446 deny->grant->allow gateway proof), which this complements.
The guardrail is deliberately conservative (total-absence only) to keep ZERO false positives.

Violations:
  SEAM001 — a declared seam's ``symbol`` appears in NO service ``src/`` (the control is unwired:
            built but called on zero request paths — it fails open).
  SEAM002 — a malformed manifest entry (missing ``symbol``) or the services root cannot be resolved
            (fail rather than vacuously pass on a broken manifest).

Run:  uv run python -m tools.lint.check_seam_wired [--manifest <path>]
Exits non-zero (1) on any violation; 0 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MANIFEST = Path(__file__).resolve().parent / "seam_wiring.yaml"


@dataclass(frozen=True)
class Violation:
    code: str
    seam: str
    message: str

    def __str__(self) -> str:
        return f"{self.seam}: {self.code} {self.message}"


def _services_referencing(services_root: Path, symbol: str) -> list[str]:
    """The service directory names whose non-test ``src/`` references ``symbol`` (substring scan).

    A plain substring scan over source text is sufficient and conservative: the symbol appearing
    anywhere non-test — import, call, attribute — counts the seam as wired in that service. Tests
    are excluded so a test-only reference cannot satisfy the runtime-wiring requirement.
    """
    referencing: list[str] = []
    for service_dir in sorted(p for p in services_root.iterdir() if p.is_dir()):
        src = service_dir / "src"
        if not src.is_dir():
            continue
        for py in src.rglob("*.py"):
            if "/tests/" in py.as_posix():
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if symbol in text:
                referencing.append(service_dir.name)
                break
    return referencing


def check(manifest_path: Path, repo_root: Path) -> list[Violation]:
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    seams: dict = data.get("seams", {}) or {}
    services_root = repo_root / "services"
    violations: list[Violation] = []

    if not services_root.is_dir():
        return [Violation("SEAM002", "<root>", f"services root not found at {services_root}")]

    for seam, spec in seams.items():
        symbol = (spec or {}).get("symbol") if isinstance(spec, dict) else None
        if not symbol:
            violations.append(
                Violation("SEAM002", seam, "manifest entry has no `symbol` to scan for")
            )
            continue
        referencing = _services_referencing(services_root, symbol)
        if not referencing:
            violations.append(
                Violation(
                    "SEAM001",
                    seam,
                    f"`{symbol}` is referenced by NO service src/ — the seam is built but wired "
                    "into zero request paths (it fails open). Wire it into the request path it is "
                    "meant to guard, or remove the manifest entry if the seam is retired.",
                )
            )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    args = parser.parse_args(argv)

    violations = check(args.manifest, args.repo_root)
    if violations:
        print("Seam-wiring guardrail FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print("Seam-wiring guardrail passed (every declared control seam is wired on a service path).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
