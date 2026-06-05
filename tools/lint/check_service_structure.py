"""Canonical service-architecture guardrail (ORAA-4 §21, R3.5).

Every service under ``services/<svc>/src/oraclous_<svc>_service/`` must follow the canonical
layered layout (see oraclous-knowledge/engineering/service-architecture-standard.md). This
checker enforces the structural invariants that the hollowness failure violated.

  STR001 — required layer dirs present: ``routes/ services/ repositories/ schema/ core/`` +
           ``main.py`` + ``app/factory.py``. (``domain/``, ``migrations/`` are optional.)
  STR002 — a file under ``routes/`` defines a non-``BaseModel`` class (the
           ``GraphNodeService``-inside-a-route anti-pattern). Only Pydantic request/response
           models may be defined in a route module.
  STR003 — a file under ``routes/`` imports a DB/Neo4j/Redis driver.
  STR004 — a DB/Neo4j/Redis driver is imported anywhere outside ``repositories/`` (exceptions:
           the ``core/`` connection layer — config/database/dependencies/lifespan — where the
           engine, sessionmaker and DI session providers are built; a ``models/`` layer, which
           holds ORM *declarations* (Mapped columns) — declaring schema is not driver/connection
           ACCESS; and a ``connectors/`` layer, whose tool executors speak a DB/HTTP protocol to an
           EXTERNAL third-party data source — that outbound driver use is the tool's payload, not
           the service's own persistence, so it is exempt. §21 rule 3, "connection setup excepted").
  STR005 — a ``*_service.py`` file sits directly under the package root (scattered/unwired
           utility drift) instead of under ``services/``.

Enforcement is OPT-IN PER SERVICE: STR violations FAIL CI only for services whose
``structure_enforced`` flag is true in ``tools/lint/service_status.yaml``. Others are reported
as informational so the rebuild can proceed service by service, unless ``--all`` is passed.

Run:  uv run python -m tools.lint.check_service_structure [--all] [<services-dir>]
Exits non-zero (1) if any ENFORCED violation is found; 0 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

_DB_DRIVER_MODULES = {
    "neo4j",
    "psycopg",
    "psycopg2",
    "asyncpg",
    "redis",
    "aioredis",
    "sqlalchemy",
    "pymysql",
}
_REQUIRED_DIRS = ("routes", "services", "repositories", "schema", "core")


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.code} {self.message}"


def _imports_db_driver(tree: ast.AST) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _DB_DRIVER_MODULES:
                    hits.append((node.lineno, root))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _DB_DRIVER_MODULES:
                hits.append((node.lineno, root))
    return hits


def _is_basemodel_class(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        name = (
            base.id
            if isinstance(base, ast.Name)
            else (base.attr if isinstance(base, ast.Attribute) else "")
        )
        if name in {"BaseModel", "BaseSettings"}:
            return True
    return False


def check_package(root: Path) -> list[Violation]:
    """root = services/<svc>/src/oraclous_<svc>_service/"""
    out: list[Violation] = []

    # STR001 — required dirs + entrypoints
    for d in _REQUIRED_DIRS:
        if not (root / d).is_dir():
            out.append(Violation(root, 0, "STR001", f"missing required layer dir '{d}/'"))
    if not (root / "main.py").exists():
        out.append(Violation(root, 0, "STR001", "missing main.py"))
    if not (root / "app" / "factory.py").exists():
        out.append(Violation(root, 0, "STR001", "missing app/factory.py"))

    for f in sorted(root.rglob("*.py")):
        rel_parts = f.relative_to(root).parts
        in_routes = "routes" in rel_parts
        in_repositories = "repositories" in rel_parts
        in_core = bool(rel_parts) and rel_parts[0] == "core"
        # ORM *declarations* (Mapped columns) may import the ORM in a dedicated `models/` layer:
        # declaring schema is not driver/connection ACCESS (that stays in repositories/ + core/).
        # Both `repositories/models.py` (colocated) and a sibling `models/` package are accepted.
        in_models = "models" in rel_parts
        # A `connectors/` layer (under domain/) holds tool executors that speak a DB/HTTP protocol
        # to an EXTERNAL, third-party data source (a user's Postgres/MySQL, a SaaS API). That
        # outbound driver use is the tool's payload, categorically different from the service's OWN
        # persistence (which stays in repositories/). Connectors never touch the app DB; they are
        # exempt from STR004 the same way models/ is. §21 rule 3.
        in_connectors = "connectors" in rel_parts
        directly_under_root = f.parent == root

        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"), filename=str(f))
        except SyntaxError as e:
            out.append(Violation(f, e.lineno or 0, "STR000", f"syntax error: {e.msg}"))
            continue

        # STR002 — non-BaseModel class in routes/
        if in_routes:
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and not _is_basemodel_class(node):
                    out.append(
                        Violation(
                            f,
                            node.lineno,
                            "STR002",
                            f"non-BaseModel class '{node.name}' in routes/ (logic -> services/)",
                        )
                    )

        # STR003 / STR004 — DB driver imports
        driver_hits = _imports_db_driver(tree)
        if driver_hits:
            if in_routes:
                for ln, mod in driver_hits:
                    out.append(
                        Violation(
                            f,
                            ln,
                            "STR003",
                            f"DB/Neo4j/Redis driver '{mod}' imported in a route module",
                        )
                    )
            elif not in_repositories and not in_core and not in_models and not in_connectors:
                for ln, mod in driver_hits:
                    out.append(
                        Violation(
                            f,
                            ln,
                            "STR004",
                            f"driver '{mod}' outside repositories/ "
                            "(only repos, core/, models/, connectors/)",
                        )
                    )

        # STR005 — scattered *_service.py directly under the package root
        if directly_under_root and f.name.endswith("_service.py") and f.name != "__init__.py":
            out.append(
                Violation(
                    f,
                    0,
                    "STR005",
                    "service-logic module sits at the package root; move it under services/",
                )
            )

    return out


def service_of(root: Path) -> str | None:
    parts = root.parts
    if "services" in parts:
        i = parts.index("services")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def discover_package_roots(services_dir: Path) -> list[Path]:
    roots: list[Path] = []
    for svc in sorted(services_dir.iterdir()):
        src = svc / "src"
        if not src.is_dir():
            continue
        for pkg in sorted(src.glob("oraclous_*_service")):
            if pkg.is_dir():
                roots.append(pkg)
    return roots


def load_status(status_path: Path) -> dict[str, dict]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return {}
    data = yaml.safe_load(status_path.read_text()) if status_path.exists() else {}
    return (data or {}).get("services", {})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Canonical service-architecture guardrail (STR001-005, ORAA-4 §21)."
    )
    ap.add_argument("services_dir", nargs="?", default="services")
    ap.add_argument("--status", default="tools/lint/service_status.yaml")
    ap.add_argument(
        "--all",
        action="store_true",
        help="enforce on ALL services regardless of structure_enforced",
    )
    args = ap.parse_args(argv)

    status = load_status(Path(args.status))
    services_dir = Path(args.services_dir)
    if not services_dir.is_dir():
        print(f"check_service_structure: no services dir at {services_dir}", file=sys.stderr)
        return 0

    enforced_failures: list[Violation] = []
    info_count = 0
    for root in discover_package_roots(services_dir):
        viols = check_package(root)
        if not viols:
            continue
        svc = service_of(root)
        enforced = args.all or (
            svc is not None and bool(status.get(svc, {}).get("structure_enforced"))
        )
        if enforced:
            enforced_failures.extend(viols)
        else:
            info_count += len(viols)

    if enforced_failures:
        print(
            "SERVICE STRUCTURE — a structure-enforced service violates the layout (ORAA-4 §21):",
            file=sys.stderr,
        )
        for v in enforced_failures:
            print(f"  {v}", file=sys.stderr)
        return 1
    if info_count:
        print(
            f"check_service_structure: {info_count} STR finding(s) (informational; not enforced)."
        )
    print("check_service_structure: no enforced structure violations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
