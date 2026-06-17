"""RLS request-binding presence guardrail — a service that runs its repositories on a
GUC-guarded RLS engine MUST also bind the organisation somewhere in its source.

The bug class: the capability-registry repositories ran on the org-GUC-guarded
``oraclous_app`` engine (``build_rls_engine`` + ``install_org_guc_guard``) but the request
path never bound the org. With FORCE'd RLS and an EMPTY ``app.current_organisation`` GUC, the
per-transaction policy resolves to "no org" — so every query returned zero rows (or raised
42501), failing the service closed at runtime. CI was green: the static RLS-coverage check
only proves the policy EXISTS, and the unit suite mocked the engine, so the missing bind never
surfaced until the real substrate ran.

This guardrail closes the *total-absence* half of that hole statically: for each service listed
in ``tools/lint/rls_coverage.yaml`` (the realized RLS set), it scans the service ``src/`` and:

  * if the service CONSTRUCTS a GUC-guarded engine — references ``build_rls_engine`` OR
    ``install_org_guc_guard`` anywhere in ``src/`` —
  * then it MUST ALSO reference at least one org-binding seam in ``src/`` — one of
    ``org_scope`` / ``use_organisation_context`` / ``enforced_organisation_id`` /
    ``bind_org_context``.

A service that builds an RLS engine with ZERO org-binding reference is flagged (RLSBIND001) —
that is exactly the capability-registry pre-fix state: a guarded engine with nothing ever
setting the GUC.

KNOWN LIMITATION (documented, by design): this is a SERVICE-LEVEL PRESENCE check. It proves a
binding seam is *present somewhere* in the service; it CANNOT prove the binding is on the
RIGHT path. It does NOT catch a PARTIAL gap — e.g. a maintenance/worker path that binds the org
while the request path runs unbound on the same guarded engine (the execution-engine split
shape). That partial class is out of scope here and is covered instead by the per-service REAL-
PATH ISOLATION TEST convention (ADR-030 §4): each realized service ships an integration test
that drives its actual request path against the real Postgres substrate and asserts cross-org
reads filter / cross-org writes are denied — proving the bind bites on the live path, which no
static check can. This guardrail is deliberately conservative (total-absence only) to keep ZERO
false positives on the realized set.

Violation:
  RLSBIND001 — a realized service builds a GUC-guarded RLS engine (``build_rls_engine`` /
               ``install_org_guc_guard``) but its ``src/`` contains NO org-binding reference
               (``org_scope`` / ``use_organisation_context`` / ``enforced_organisation_id`` /
               ``bind_org_context``). The guarded engine will fail-close to zero rows / 42501.
  RLSBIND002 — the manifest names a service whose directory or ``src`` cannot be resolved
               (stale manifest entry — fail rather than vacuously pass).

Run:  uv run python -m tools.lint.check_rls_request_binding [--manifest <path>]
Exits non-zero (1) on any violation; 0 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MANIFEST = Path(__file__).resolve().parent / "rls_coverage.yaml"

# Constructing a GUC-guarded engine = a transaction-scoped org GUC is installed on it.
_ENGINE_TOKENS = ("build_rls_engine", "install_org_guc_guard")
# Binding the org = setting the org context the GUC reads from (per-request or per-task).
_BINDING_TOKENS = (
    "org_scope",
    "use_organisation_context",
    "enforced_organisation_id",
    "bind_org_context",
)


@dataclass(frozen=True)
class Violation:
    code: str
    service: str
    message: str

    def __str__(self) -> str:
        return f"{self.service}: {self.code} {self.message}"


def _src_references_any(service_src: Path, tokens: tuple[str, ...]) -> bool:
    """True if any ``*.py`` under the service src contains any of the given identifier tokens.

    A plain substring scan over source text is sufficient here (and conservative): a token like
    ``org_scope`` appearing anywhere — import, call, or attribute — counts as the seam being
    present. Tests are excluded so a test-only reference cannot satisfy the runtime requirement.
    """
    for py in service_src.rglob("*.py"):
        if "/tests/" in py.as_posix():
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(token in text for token in tokens):
            return True
    return False


def _service_src(service_dir: Path) -> Path | None:
    """Resolve ``services/<svc>/src`` (the source root scanned for tokens)."""
    src = service_dir / "src"
    return src if src.is_dir() else None


def check(manifest_path: Path, repo_root: Path) -> list[Violation]:
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    services: dict = data.get("services", {}) or {}
    violations: list[Violation] = []

    for service in services:
        service_dir = repo_root / "services" / service
        if not service_dir.is_dir():
            violations.append(
                Violation(
                    "RLSBIND002", service, f"manifest names a missing service dir {service_dir}"
                )
            )
            continue
        src = _service_src(service_dir)
        if src is None:
            violations.append(
                Violation("RLSBIND002", service, f"cannot resolve a src dir under {service_dir}")
            )
            continue

        builds_guarded_engine = _src_references_any(src, _ENGINE_TOKENS)
        if not builds_guarded_engine:
            # No GUC-guarded engine constructed here — this guardrail has nothing to assert.
            continue

        if not _src_references_any(src, _BINDING_TOKENS):
            violations.append(
                Violation(
                    "RLSBIND001",
                    service,
                    "constructs a GUC-guarded RLS engine (build_rls_engine / "
                    "install_org_guc_guard) but its src/ has NO org-binding reference "
                    f"({' / '.join(_BINDING_TOKENS)}) — the guarded engine will fail-close to "
                    "zero rows / 42501 with an empty org GUC (the capreg pre-fix state). Bind the "
                    "org on the request/task path before any query.",
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
        print("RLS request-binding guardrail FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print(
        "RLS request-binding guardrail passed "
        "(every RLS-engine service binds the org somewhere in src)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
