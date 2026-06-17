"""Service first-party-dependency guardrail (catches the #375 class) — every first-party
workspace package a service IMPORTS is also DECLARED as a dependency of that service.

The #375 bug: the application-gateway service ``import``ed ``oraclous_substrate`` in its
source but never declared ``oraclous-substrate`` in its ``pyproject.toml``. CI's single
workspace venv (``uv sync --all-packages``) installs *every* workspace package regardless of
who declares it, so the import resolved at lint/test time and CI stayed green — but the
per-service built image installs ONLY that service's declared dependencies, so the running
container raised ``ModuleNotFoundError`` at deploy/runtime. A green CI masked a broken image.

This guardrail closes that blind spot statically (no venv, no build): for each service under
``services/*`` it parses every module under ``src/`` and, for each ``import`` of a FIRST-PARTY
workspace package, asserts that package is declared in the service's ``pyproject.toml`` BOTH:

  * in ``[project.dependencies]`` (so the built wheel installs it), AND
  * in ``[tool.uv.sources]`` with ``workspace = true`` (so it resolves to the in-repo
    package, not a — non-existent — PyPI release).

The first-party set is DERIVED, never hardcoded: it is the import name (the single package dir
under ``packages/<p>/``, e.g. ``oraclous_substrate``) paired with the distribution name
(``packages/<p>/pyproject.toml`` ``[project].name``, e.g. ``oraclous-substrate``) for every
``packages/*`` distribution. A new workspace package is picked up automatically.

Scope: only first-party ``oraclous_*`` / ``packages/*`` imports are checked. Stdlib and
third-party imports are ignored (those are out of this guardrail's remit — a missing
third-party dep fails the build/import anyway; the masked class is specifically the
workspace-venv one). A service's OWN ``src`` package is its own code, not a dependency, so it
is excluded from the first-party set per service.

Violation:
  DEP001 — a service's ``src`` imports first-party package ``<import>`` (dist ``<dist>``) that
           is not declared in BOTH ``[project.dependencies]`` and ``[tool.uv.sources]
           (workspace = true)`` of that service's ``pyproject.toml``. The message names which
           of the two halves is missing.

Run:  uv run python -m tools.lint.check_service_dep_imports [services ...]
Exits non-zero (1) on any violation; 0 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Violation:
    code: str
    service: str
    message: str

    def __str__(self) -> str:
        return f"{self.service}: {self.code} {self.message}"


def _package_dirs(packages_root: Path) -> list[Path]:
    """Every ``packages/<p>/`` distribution directory (one that has a pyproject.toml)."""
    if not packages_root.is_dir():
        return []
    return sorted(
        p for p in packages_root.iterdir() if p.is_dir() and (p / "pyproject.toml").is_file()
    )


def _dist_name(pyproject: Path) -> str | None:
    """The ``[project].name`` (distribution name, e.g. ``oraclous-substrate``) of a pyproject."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    name = (data.get("project") or {}).get("name")
    return str(name) if isinstance(name, str) else None


def _import_package_name(package_dir: Path) -> str | None:
    """The single import-name package directory under a distribution (e.g. ``oraclous_substrate``).

    A workspace package lays its importable package under either ``packages/<p>/<pkg>/`` or
    ``packages/<p>/src/<pkg>/``. Return that ``<pkg>`` (the name used in ``import <pkg>``).
    """
    src = package_dir / "src"
    search_root = src if src.is_dir() else package_dir
    candidates = [
        d
        for d in search_root.iterdir()
        if d.is_dir() and (d / "__init__.py").is_file() and d.name != "tests"
    ]
    return candidates[0].name if len(candidates) == 1 else None


def first_party_index(repo_root: Path) -> dict[str, str]:
    """Map of import-name -> distribution-name for every first-party workspace package.

    Derived from ``packages/*`` — never hardcoded. e.g. ``{"oraclous_substrate":
    "oraclous-substrate", ...}``.
    """
    index: dict[str, str] = {}
    for pkg_dir in _package_dirs(repo_root / "packages"):
        import_name = _import_package_name(pkg_dir)
        dist_name = _dist_name(pkg_dir / "pyproject.toml")
        if import_name and dist_name:
            index[import_name] = dist_name
    return index


def _imported_top_levels(py: Path) -> set[str]:
    """The set of top-level imported module names in a Python file (AST-parsed, so comments
    and strings can never trip a false positive). ``import a.b`` / ``from a.b import c`` both
    yield ``a``; a relative import (``from . import x``) yields nothing."""
    tops: set[str] = set()
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return tops
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                tops.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import (intra-service) — never a workspace dep.
            if node.level == 0 and node.module:
                tops.add(node.module.split(".", 1)[0])
    return tops


def _service_src_imports(service_dir: Path) -> set[str]:
    """Every top-level module name imported anywhere under the service's ``src/``."""
    src = service_dir / "src"
    if not src.is_dir():
        return set()
    imports: set[str] = set()
    for py in src.rglob("*.py"):
        imports |= _imported_top_levels(py)
    return imports


def _declared_deps(pyproject: Path) -> tuple[set[str], set[str]]:
    """``([project.dependencies] dist names, [tool.uv.sources] workspace=true dist names)``.

    Dependency specifiers are normalised to their bare distribution name (the leading run of
    name characters before any version/extra marker, e.g. ``oraclous-substrate>=1`` ->
    ``oraclous-substrate``).
    """
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return set(), set()
    deps_raw = (data.get("project") or {}).get("dependencies") or []
    declared: set[str] = set()
    for spec in deps_raw:
        if isinstance(spec, str):
            name = spec.strip().split(";", 1)[0]
            for sep in ("[", "=", "<", ">", "!", "~", " ", "("):
                name = name.split(sep, 1)[0]
            name = name.strip()
            if name:
                declared.add(name)
    sources_raw = (data.get("tool") or {}).get("uv", {}).get("sources") or {}
    workspace: set[str] = set()
    if isinstance(sources_raw, dict):
        for dist, cfg in sources_raw.items():
            if isinstance(cfg, dict) and cfg.get("workspace") is True:
                workspace.add(str(dist))
    return declared, workspace


def check(repo_root: Path, service_dirs: list[Path]) -> list[Violation]:
    index = first_party_index(repo_root)
    violations: list[Violation] = []

    for service_dir in service_dirs:
        service = service_dir.name
        pyproject = service_dir / "pyproject.toml"
        if not pyproject.is_file():
            continue
        # A service's own src package is its own code, not a workspace dependency — exclude it.
        own_pkg = _service_own_package(service_dir)
        declared, workspace = _declared_deps(pyproject)

        imported = _service_src_imports(service_dir)
        for import_name in sorted(imported):
            if import_name not in index or import_name == own_pkg:
                continue  # not first-party, or the service's own package
            dist = index[import_name]
            missing: list[str] = []
            if dist not in declared:
                missing.append("[project.dependencies]")
            if dist not in workspace:
                missing.append("[tool.uv.sources] (workspace = true)")
            if missing:
                violations.append(
                    Violation(
                        "DEP001",
                        service,
                        f"src imports first-party package {import_name!r} (dist {dist!r}) but it "
                        f"is not declared in {', '.join(missing)} of {pyproject.name}. CI's "
                        "workspace venv masks this; the built image will ModuleNotFound it "
                        "(the #375 class). Add the dependency.",
                    )
                )

    return violations


def _service_own_package(service_dir: Path) -> str | None:
    """The service's own ``src/<pkg>`` import-name (its own code, never a dependency)."""
    src = service_dir / "src"
    if not src.is_dir():
        return None
    candidates = [
        d
        for d in src.iterdir()
        if d.is_dir() and (d / "__init__.py").is_file() and d.name != "tests"
    ]
    return candidates[0].name if len(candidates) == 1 else None


def _resolve_service_dirs(repo_root: Path, args_services: list[str]) -> list[Path]:
    """Resolve the service directories to scan. With no args, every ``services/*`` dir with a
    pyproject; otherwise each named path (relative to repo root or absolute)."""
    if not args_services:
        services_root = repo_root / "services"
        if not services_root.is_dir():
            return []
        return sorted(
            p for p in services_root.iterdir() if p.is_dir() and (p / "pyproject.toml").is_file()
        )
    dirs: list[Path] = []
    for s in args_services:
        p = Path(s)
        if not p.is_absolute():
            p = repo_root / p
        if p.is_dir() and (p / "pyproject.toml").is_file():
            dirs.append(p)
        elif p.name == "services" and p.is_dir():
            dirs.extend(
                sorted(c for c in p.iterdir() if c.is_dir() and (c / "pyproject.toml").is_file())
            )
    return dirs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "services",
        nargs="*",
        help="Service dirs (or the 'services' root) to scan; defaults to every services/*.",
    )
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    args = parser.parse_args(argv)

    service_dirs = _resolve_service_dirs(args.repo_root, args.services)
    violations = check(args.repo_root, service_dirs)
    if violations:
        print("Service first-party-dependency guardrail FAILED (#375 class):", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print(
        "Service first-party-dependency guardrail passed "
        "(every imported workspace package is declared)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
