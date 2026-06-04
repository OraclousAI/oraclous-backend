"""Hollowness audit (ORAA-4 §23, R3.5) — produce a true-completion map of the services.

Read-only. This is the report behind the R3.5 decision that R2/R3 shipped hollow: for each
service it measures the mechanical signals of hollowness and prints a per-service verdict
(``HOLLOW`` vs ``PLAUSIBLY REAL``). It does NOT edit code or the board — it tells the truth so a
human can re-open the hollow stories.

Per service it reports:
  * non-test source LOC and route-endpoint count;
  * HOL markers (NotImplementedError / _stub_ / 501 / deferral / stub bodies — via check_no_stubs);
  * stub classes (a class whose every method is a trivial stub body) — the GraphNodeService shape;
  * whether any route handler's module reaches a repositories/ layer at all.

Verdict heuristic (a signal, not a proof): HOLLOW if it has HOL markers OR a stub class OR has
route endpoints but no repositories/ layer; otherwise PLAUSIBLY REAL (verify via the §22 smoke).

Run:  uv run python -m tools.audit.hollowness_audit [services]
Always exits 0 (it is a report, not a gate).
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from tools.lint.check_no_stubs import check_source, is_non_test_src
from tools.lint.check_service_structure import discover_package_roots, service_of

_ROUTE_DECOS = {"get", "post", "put", "patch", "delete"}


def _count_loc(root: Path) -> int:
    total = 0
    for f in root.rglob("*.py"):
        if is_non_test_src(f):
            total += sum(
                1
                for ln in f.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip()
            )
    return total


def _count_endpoints(root: Path) -> int:
    n = 0
    for f in root.rglob("*.py"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    if (
                        isinstance(d, ast.Call)
                        and isinstance(d.func, ast.Attribute)
                        and d.func.attr in _ROUTE_DECOS
                    ):
                        n += 1
    return n


def _is_interface_class(cls: ast.ClassDef) -> bool:
    """A Protocol/ABC interface — trivial (`...`) method bodies are the interface, not stubs."""
    from tools.lint.check_no_stubs import _decorator_names

    for base in cls.bases:
        name = (
            base.id
            if isinstance(base, ast.Name)
            else (base.attr if isinstance(base, ast.Attribute) else "")
        )
        if name in {"Protocol", "ABC"}:
            return True
    methods = [m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return bool(methods) and all("abstractmethod" in _decorator_names(m) for m in methods)


def _stub_classes(root: Path) -> list[str]:
    """Classes (outside Protocol/ABC) whose every method body is a trivial stub."""
    from tools.lint.check_no_stubs import _trivial_body  # reuse the same definition

    found: list[str] = []
    for f in root.rglob("*.py"):
        if not is_non_test_src(f):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and not _is_interface_class(node):
                methods = [
                    m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                if methods and all(_trivial_body(m) for m in methods):
                    found.append(f"{node.name} ({f.relative_to(root)})")
    return found


def _has_repositories(root: Path) -> bool:
    repo = root / "repositories"
    return repo.is_dir() and any(repo.rglob("*.py"))


def _hol_count(root: Path) -> int:
    n = 0
    for f in root.rglob("*.py"):
        if is_non_test_src(f):
            n += len(check_source(f, f.read_text(encoding="utf-8", errors="replace")))
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hollowness audit (ORAA-4 §23).")
    ap.add_argument("services_dir", nargs="?", default="services")
    args = ap.parse_args(argv)
    services_dir = Path(args.services_dir)
    if not services_dir.is_dir():
        print(f"no services dir at {services_dir}", file=sys.stderr)
        return 0

    print("R3.5 HOLLOWNESS AUDIT — true-completion map (ORAA-4 §23)\n")
    print(
        f"{'service':<30} {'LOC':>6} {'endpts':>6} {'HOL':>4} {'stubcls':>7} {'repos':>5} verdict"
    )
    print("-" * 86)
    for root in discover_package_roots(services_dir):
        svc = service_of(root) or root.name
        loc = _count_loc(root)
        endpoints = _count_endpoints(root)
        hol = _hol_count(root)
        stubcls = _stub_classes(root)
        has_repo = _has_repositories(root)
        repo_flag = "yes" if has_repo else "no"
        hollow = bool(hol) or bool(stubcls) or (endpoints > 0 and not has_repo)
        verdict = "HOLLOW" if hollow else "PLAUSIBLY REAL (verify via §22 smoke)"
        print(
            f"{svc:<30} {loc:>6} {endpoints:>6} {hol:>4} {len(stubcls):>7} {repo_flag:>5} {verdict}"
        )
        for sc in stubcls:
            print(f"{'':<32}   stub class: {sc}")
    print("\nVerdict is a mechanical signal, not a proof. A service is done only by the §22 gate")
    print("(incl. Reza smoke sign-off). Re-open every HOLLOW 'done' story under R3.5.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
