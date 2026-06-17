"""RLS-coverage guardrail (ADR-030 §4) — every org-scoped table in a REALIZED service
has the Postgres row-level-security backstop applied.

ADR-012 §2 committed to RLS as the defense-in-depth backstop; ADR-030 realizes it
service-by-service. This guardrail is the recurrence mechanism (ORAA-4 §20): once a
service is realized, a new org-scoped table that ships WITHOUT RLS — or a table that
drops its ``enable_rls_on`` call — fails CI, rather than silently leaving a row-level
hole behind the app-layer ``WHERE``.

It is **scope-aware**: it enforces only the services listed in
``tools/lint/rls_coverage.yaml`` (the realized set). A not-yet-realized service is
ignored, so the phased rollout doesn't break the build before its slice lands.

Two static checks per realized service (no DB needed — suitable for the CI lint job;
the data-layer proof that RLS actually *bites* is the per-service integration isolation
test):

  RLS001 — a manifest table has no ``enable_rls_on("<table>")`` call in the service's
           ``migrations/`` (the table is declared org-scoped but RLS is never applied).
  RLS002 — a realized service declares an org-scoped storage model (a class with
           ``__tablename__`` AND an ``organisation_id`` column) whose table is ABSENT
           from the manifest — a new org-scoped table that would dodge the backstop.
           A table listed under the service's documented ``exclusions`` (each with a
           ``reason``) is exempt: some org-scoped tables are deliberately NOT RLS-d
           because they are read WITHOUT a bound org (e.g. auth's ``org_members``,
           enumerated across a user's orgs at login — RLS would fail-close login). An
           exclusion makes that decision explicit and reviewable rather than silent.
  RLS003 — the manifest names a service directory or table model that does not exist,
           OR an ``exclusions`` entry matches no org-scoped model / is also in ``tables``
           (a stale or contradictory manifest entry — fail rather than vacuously pass).

Run:  uv run python -m tools.lint.check_rls_coverage [--manifest <path>]
Exits non-zero (1) on any violation; 0 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MANIFEST = Path(__file__).resolve().parent / "rls_coverage.yaml"
_ORG_COLUMNS = {"organisation_id", "organization_id"}


@dataclass(frozen=True)
class Violation:
    code: str
    service: str
    message: str

    def __str__(self) -> str:
        return f"{self.service}: {self.code} {self.message}"


def _exclusions_of(spec: dict | None) -> set[str]:
    """The set of documented-excluded table names from a service's manifest spec.

    Accepts either a mapping (``{table: reason}``) or a list of ``{table, reason}`` dicts under
    ``exclusions`` — the reason is for the human reader/reviewer; this checker only needs the names.
    """
    raw = (spec or {}).get("exclusions") or {}
    if isinstance(raw, dict):
        return {str(t) for t in raw}
    if isinstance(raw, list):
        names: set[str] = set()
        for item in raw:
            if isinstance(item, dict) and "table" in item:
                names.add(str(item["table"]))
            elif isinstance(item, str):
                names.add(item)
        return names
    return set()


def _assigned_names_and_value(stmt: ast.stmt) -> tuple[list[str], ast.expr | None]:
    """The simple ``Name`` targets a statement assigns plus its RHS value (an
    ``Assign``/``AnnAssign``), else ``([], None)``."""
    if isinstance(stmt, ast.Assign):
        names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
        return names, stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return [stmt.target.id], stmt.value
    return [], None


def _tablename_of(cls: ast.ClassDef) -> str | None:
    """Return the ``__tablename__`` string literal a model class assigns, or None."""
    for stmt in cls.body:
        names, value = _assigned_names_and_value(stmt)
        if "__tablename__" in names and isinstance(value, ast.Constant):
            if isinstance(value.value, str):
                return value.value
    return None


def _class_declares_org_column(cls: ast.ClassDef) -> bool:
    """True if the class body assigns an ``organisation_id`` (mapped) attribute."""
    for stmt in cls.body:
        names, _value = _assigned_names_and_value(stmt)
        if any(name in _ORG_COLUMNS for name in names):
            return True
    return False


def _org_scoped_tables_declared(service_src: Path) -> set[str]:
    """Every table name declared by an org-scoped storage model under the service src.

    A storage model is a class with ``__tablename__``; org-scoped means it also declares
    an ``organisation_id`` column. Returns the set of such ``__tablename__`` values.
    """
    tables: set[str] = set()
    for py in service_src.rglob("*.py"):
        if "/migrations/" in py.as_posix() or "/tests/" in py.as_posix():
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                table = _tablename_of(node)
                if table is not None and _class_declares_org_column(node):
                    tables.add(table)
    return tables


def _enable_rls_calls(migrations_dir: Path) -> set[str]:
    """Every ``enable_rls_on(..., "<table>")`` table-literal argument across the service's
    migrations. Recognises the table as the first string-literal positional arg (matching
    ``enable_rls_on(conn, "user_credentials")``). Robust to it being passed via a list
    constant the migration iterates (``for t in _RLS_TABLES: enable_rls_on(raw, t)``):
    those literals are collected from any string-list assigned in the migration too.
    """
    covered: set[str] = set()
    if not migrations_dir.is_dir():
        return covered
    for py in migrations_dir.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (SyntaxError, UnicodeDecodeError):
            continue
        has_enable_call = False
        list_literals: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else ""
                )
                if name == "enable_rls_on":
                    has_enable_call = True
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            covered.add(arg.value)
            # collect string-list/tuple literals (a table registry the migration loops over)
            if isinstance(node, (ast.List, ast.Tuple)):
                for elt in node.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        list_literals.add(elt.value)
        # Only credit list literals when the file actually invokes enable_rls_on (so an
        # unrelated string list elsewhere can't spuriously satisfy coverage).
        if has_enable_call:
            covered |= list_literals
    return covered


def _service_src(service_dir: Path) -> Path | None:
    """Resolve ``services/<svc>/src/<pkg>`` (the single src package dir)."""
    src = service_dir / "src"
    if not src.is_dir():
        return None
    pkgs = [p for p in src.iterdir() if p.is_dir() and (p / "__init__.py").exists()]
    return pkgs[0] if len(pkgs) == 1 else src


def check(manifest_path: Path, repo_root: Path) -> list[Violation]:
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    services: dict = data.get("services", {}) or {}
    violations: list[Violation] = []

    for service, spec in services.items():
        manifest_tables = set((spec or {}).get("tables", []) or [])
        # Documented per-service exclusions: org-scoped tables deliberately NOT RLS-d, each with a
        # human reason. Accepts a mapping {table: reason} or a list of {table, reason} dicts.
        excluded_tables = _exclusions_of(spec)
        service_dir = repo_root / "services" / service
        if not service_dir.is_dir():
            violations.append(
                Violation("RLS003", service, f"manifest names a missing service dir {service_dir}")
            )
            continue

        # A table cannot be both RLS-enabled and excluded — that is a contradictory manifest.
        for table in sorted(manifest_tables & excluded_tables):
            violations.append(
                Violation(
                    "RLS003",
                    service,
                    f"table {table!r} is in BOTH 'tables' and 'exclusions' — a table is either "
                    "RLS-enabled or documented-excluded, never both",
                )
            )

        # RLS001 — every manifest table is applied via enable_rls_on in migrations/.
        covered = _enable_rls_calls(service_dir / "migrations")
        for table in sorted(manifest_tables - covered):
            violations.append(
                Violation(
                    "RLS001",
                    service,
                    f"org-scoped table {table!r} has no enable_rls_on(...) call in migrations/ "
                    "(RLS backstop not applied — ADR-030 §1/§4)",
                )
            )

        # RLS002 — no org-scoped storage model declares a table missing from the manifest.
        src = _service_src(service_dir)
        if src is None:
            violations.append(
                Violation("RLS003", service, f"cannot resolve a src package under {service_dir}")
            )
            continue
        declared = _org_scoped_tables_declared(src)
        # An org-scoped model is accounted for iff it is RLS-enabled (in `tables`) OR documented as
        # an explicit exclusion. Anything else dodges the backstop silently → RLS002.
        for table in sorted(declared - manifest_tables - excluded_tables):
            violations.append(
                Violation(
                    "RLS002",
                    service,
                    f"org-scoped model table {table!r} is not in the RLS manifest; a realized "
                    "service must either RLS-enable every org-scoped table (add it to 'tables' + "
                    "an enable_rls_on migration) or document it under 'exclusions' with a reason "
                    "(an org-scoped table reached without a bound org — see ADR-030)",
                )
            )

        # RLS003 — a manifest table with no matching org-scoped model (stale entry).
        for table in sorted(manifest_tables - declared):
            violations.append(
                Violation(
                    "RLS003",
                    service,
                    f"manifest table {table!r} matches no org-scoped storage model under {src} "
                    "(stale manifest entry)",
                )
            )

        # RLS003 — an exclusion that matches no org-scoped model (stale/typo'd exclusion). Excluding
        # a table that isn't actually a declared org-scoped model is meaningless and likely a typo.
        for table in sorted(excluded_tables - declared):
            violations.append(
                Violation(
                    "RLS003",
                    service,
                    f"excluded table {table!r} matches no org-scoped storage model under {src} "
                    "(stale or typo'd exclusion — an exclusion must name a real org-scoped table)",
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
        print("RLS-coverage guardrail FAILED (ADR-030 §4):", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print("RLS-coverage guardrail passed (every realized org-scoped table has RLS applied).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
