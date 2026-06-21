"""Static guardrail for TDD-window test-import hygiene (R0.5 story 0b follow-up).

A ``[tests]`` PR legitimately lands tests for an intra-repo seam before its
implementation exists (the TDD contract, CLAUDE.md §4.1). If such a test imports
the not-yet-built seam at *module level*, pytest aborts collection (exit 2) for
the whole run — reddening every open PR's quality / integration / security gate,
not just its own, until the paired ``[impl]`` lands.

The convention (security-architect coverage-safety concurrence): import a
not-yet-built intra-repo seam *function-locally* (inside the test or fixture), so
the module collects cleanly and the test fails at *runtime* with
``ModuleNotFoundError`` — RED-by-design, on its own marker only, never masking
coverage. A function-local import *relocates* an import failure from collection
time to run time; it does not suppress it.

This guardrail enforces that convention and, critically, forbids the two ways a
missing seam can be turned into a *green skip* instead of a red failure
(``importorskip`` / ``try/except ImportError`` skip of an intra-repo seam). For a
``security``-marked test a green skip would hide an unverified threat behind a
green gate — the precise failure mode this rule exists to prevent.

Rules (best-effort AST + import-resolution heuristics — guardrails, not proofs):

  TST001 — a *module-level* import of an intra-repo ``oraclous_*`` seam that is
           NOT importable on the current tree (would abort collection). Steer it
           function-local. Built seams and function-local imports pass clean; the
           rule self-clears once the ``[impl]`` lands.
  TST002 — a ``pytest.importorskip("oraclous_*")`` or a
           ``try: import oraclous_* … except ImportError: pytest.skip(...)`` of an
           intra-repo seam — forbidden anywhere under tests: it converts a missing
           seam into a green skip (masked coverage). A missing intra-repo seam
           must hard-fail, never skip.

Best-effort limit: TST001 resolves importability by importing the module in the
current environment (the same thing collection does); a built seam whose package
``__init__`` raises at import for environmental reasons could be a false positive.
The authoritative control is the gate itself; this rule shrinks its blast radius.

Run:  uv run python -m tools.lint.check_test_imports <path> [<path> ...]
Exits non-zero (1) if any violation is found; 0 otherwise.
"""

from __future__ import annotations

import ast
import importlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Intra-repo packages are exactly the ``oraclous_*`` roots (the import-linter
# root_packages). Using the prefix rather than a fixed list auto-covers new
# packages; there are no third-party ``oraclous_*`` dependencies.
INTRA_REPO_PREFIX = "oraclous_"
IMPORT_ERROR_NAMES = {"ImportError", "ModuleNotFoundError"}
SKIP_CALL_ATTRS = {"skip", "importorskip"}
SKIP_DIRS = {".venv", "venv", "__pycache__", "build", "dist", ".git"}

# A resolver answers: is ``module`` importable on the current tree, and do the
# imported ``names`` (if any) resolve off it? Injectable so the unit tests are
# deterministic without depending on which seams happen to be built.
Resolver = Callable[[str, "tuple[str, ...]"], bool]


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def _is_intra_repo(module: str) -> bool:
    return bool(module) and module.split(".")[0].startswith(INTRA_REPO_PREFIX)


def _module_importable(module: str, names: tuple[str, ...]) -> bool:
    """Best-effort: mirror what ``from module import *names*`` does at collection
    time. Any failure (module absent, symbol absent) → not importable → flag."""
    try:
        mod = importlib.import_module(module)
    except Exception:
        return False
    for name in names:
        if hasattr(mod, name):
            continue
        try:  # ``name`` may be a submodule not yet bound on the package
            importlib.import_module(f"{module}.{name}")
        except Exception:
            return False
    return True


def _is_skip_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in SKIP_CALL_ATTRS
    return isinstance(func, ast.Name) and func.id in SKIP_CALL_ATTRS


def _handler_catches_import_error(handlers: list[ast.ExceptHandler]) -> bool:
    for h in handlers:
        if h.type is None:  # bare except
            return True
        types = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
        for t in types:
            if isinstance(t, ast.Name) and t.id in IMPORT_ERROR_NAMES:
                return True
    return False


def _first_intra_repo_import(stmts: list[ast.stmt]) -> str | None:
    for s in stmts:
        if isinstance(s, ast.Import):
            for alias in s.names:
                if _is_intra_repo(alias.name):
                    return alias.name
        elif isinstance(s, ast.ImportFrom) and s.level == 0 and _is_intra_repo(s.module or ""):
            return s.module
    return None


def _handlers_skip(handlers: list[ast.ExceptHandler]) -> bool:
    for h in handlers:
        for sub in ast.walk(h):
            if isinstance(sub, ast.Call) and _is_skip_call(sub):
                return True
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str, resolver: Resolver) -> None:
        self.path = path
        self.resolver = resolver
        self.violations: list[Violation] = []
        self._func_depth = 0
        # Import nodes inside a handled ``try/except ImportError`` are reported as
        # TST002 (the masking construct), not double-counted as TST001.
        self._skip_tst001: set[int] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._func_depth += 1
        self.generic_visit(node)
        self._func_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _flag(self, rule: str, line: int, message: str) -> None:
        self.violations.append(Violation(rule, self.path, line, message))

    def visit_Import(self, node: ast.Import) -> None:
        if self._func_depth == 0 and id(node) not in self._skip_tst001:
            for alias in node.names:
                if _is_intra_repo(alias.name) and not self.resolver(alias.name, ()):
                    self._flag(
                        "TST001",
                        node.lineno,
                        f"module-level import of not-yet-built intra-repo seam "
                        f"'{alias.name}' — import it function-locally so collection "
                        f"does not abort (it fails RED at runtime instead)",
                    )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if (
            self._func_depth == 0
            and node.level == 0
            and id(node) not in self._skip_tst001
            and _is_intra_repo(node.module or "")
        ):
            names = tuple(a.name for a in node.names if a.name != "*")
            if not self.resolver(node.module or "", names):
                self._flag(
                    "TST001",
                    node.lineno,
                    f"module-level import of not-yet-built intra-repo seam "
                    f"'{node.module}' — import it function-locally so collection "
                    f"does not abort (it fails RED at runtime instead)",
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        is_importorskip = (isinstance(func, ast.Attribute) and func.attr == "importorskip") or (
            isinstance(func, ast.Name) and func.id == "importorskip"
        )
        if is_importorskip and node.args:
            arg = node.args[0]
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and _is_intra_repo(arg.value)
            ):
                self._flag(
                    "TST002",
                    node.lineno,
                    f"importorskip('{arg.value}') of an intra-repo seam converts a "
                    f"missing seam into a green skip — masked coverage; a missing "
                    f"intra-repo seam must hard-fail (import it function-locally instead)",
                )
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        module = _first_intra_repo_import(node.body)
        if (
            module
            and _handler_catches_import_error(node.handlers)
            and _handlers_skip(node.handlers)
        ):
            for s in node.body:
                if isinstance(s, ast.Import | ast.ImportFrom):
                    self._skip_tst001.add(id(s))
            self._flag(
                "TST002",
                node.lineno,
                f"try/except ImportError that skips on a missing intra-repo seam "
                f"'{module}' converts missing coverage into a green skip — a missing "
                f"intra-repo seam must hard-fail (import it function-locally instead)",
            )
        self.generic_visit(node)


def check_source(
    source: str,
    path: str = "<string>",
    *,
    resolver: Resolver = _module_importable,
) -> list[Violation]:
    visitor = _Visitor(path, resolver)
    visitor.visit(ast.parse(source, filename=path))
    return visitor.violations


def _is_test_file(p: Path) -> bool:
    return "tests" in p.parts


def check_paths(paths: list[str], *, resolver: Resolver = _module_importable) -> list[Violation]:
    out: list[Violation] = []
    for raw in paths:
        root = Path(raw)
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for f in files:
            if any(part in SKIP_DIRS for part in f.parts) or not _is_test_file(f):
                continue
            out.extend(check_source(f.read_text(encoding="utf-8"), str(f), resolver=resolver))
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    paths = args or ["packages", "services", "tests"]
    violations = check_paths(paths)
    for v in violations:
        print(v)
    if violations:
        print(f"\n{len(violations)} test-import hygiene violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
