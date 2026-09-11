"""#826 (24 August 2026 solution-architect ruling, item 5) — the ``provenance-on-dispatch`` seam.

``tools/lint/seam_wiring.yaml`` declares one seam today (``rebac-cross-org-admission``) — exactly
the built-but-unwired failure class ``check_seam_wired`` exists to catch (#446/#456). The ruling
asks for a SECOND declared seam: a dispatch that produces a provenance record, wired through the
symbol ``emit_dispatch_provenance``, present on at least one service request path.

This test runs the REAL checker (``tools/lint/check_seam_wired.check`` — already built, not a
not-yet-built seam) against the REAL repo manifest + services tree, so it stays honest about
whether the seam is actually wired anywhere, not a synthetic fixture. RED until both (a) the
manifest entry exists and (b) ``emit_dispatch_provenance`` is referenced by some service ``src/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from tools.lint.check_seam_wired import check

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _REPO_ROOT / "tools" / "lint" / "seam_wiring.yaml"

_SEAM = "provenance-on-dispatch"
_SYMBOL = "emit_dispatch_provenance"


def test_provenance_on_dispatch_is_declared_and_wired() -> None:
    """Both halves of the guardrail, in one test so neither half can pass vacuously: the manifest
    entry must exist AND the checker must find the symbol wired on a real service path."""
    data = yaml.safe_load(_MANIFEST.read_text(encoding="utf-8")) or {}
    seams = data.get("seams", {}) or {}
    assert _SEAM in seams, f"{_MANIFEST} has no {_SEAM!r} entry yet"
    assert seams[_SEAM].get("symbol") == _SYMBOL, seams[_SEAM]

    violations = check(_MANIFEST, _REPO_ROOT)
    offending = [v for v in violations if v.seam == _SEAM]
    assert offending == [], offending
