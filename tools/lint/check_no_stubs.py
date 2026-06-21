"""No-hollow guardrail (R3.5) — a service marked done may contain no stubs.

R2/R3 shipped hollow: stub endpoints, ``raise NotImplementedError``, a ``GraphNodeService``
stub class inside a route file. Those stories passed the Definition of Done because the tests
were written against the stubs. This checker is the mechanism that makes that
impossible: a service cannot be flipped to ``claimed_done: true`` in
``tools/lint/service_status.yaml`` while any HOL marker remains in its non-test source.

  HOL001 — ``raise NotImplementedError`` in non-test src.
  HOL002 — a ``_stub_``/``stub_result`` identifier or a string containing ``_stub_``.
  HOL003 — a comment/string matching ``TODO: implement`` / ``not yet implemented`` /
           ``deferred to R<n>`` (the "I'll do it later" markers).
  HOL004 — a non-abstract function/method whose body (after an optional docstring) is ONLY
           ``pass``, ``...``, or ``return None|[]|{}|False`` — i.e. a stub body. Excludes
           ``@abstractmethod``/``@overload`` and methods of a ``Protocol`` subclass.
  HOL005 — a route handler returning HTTP ``501`` / ``HTTP_501_NOT_IMPLEMENTED``.

Enforcement is OPT-IN PER SERVICE: HOL violations FAIL CI only for services whose
``claimed_done`` flag is true. Other services are reported as informational (so the rebuild can
proceed service by service without breaking the build), unless ``--all`` is passed.

Run:  uv run python -m tools.lint.check_no_stubs [--all] [<path> ...]  (default path: services)
Exits non-zero (1) if any ENFORCED violation is found; 0 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_HOL003_RE = re.compile(r"todo:\s*implement|not yet implemented|deferred to r\d", re.IGNORECASE)
_HOL002_RE = re.compile(r"_stub_|stub_result|\b_stub\b")
_TRIVIAL_RETURNS = {"None", "[]", "{}", "False", "()"}


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.code} {self.message}"


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _decorator_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for d in func.decorator_list:
        if isinstance(d, ast.Name):
            names.add(d.id)
        elif isinstance(d, ast.Attribute):
            names.add(d.attr)
        elif isinstance(d, ast.Call):
            f = d.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def _trivial_body(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the body is only a stub: pass / ... / return None|[]|{}|False (after a docstring)."""
    body = list(func.body)
    if body and _is_docstring(body[0]):
        body = body[1:]
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is ...
    ):
        return True
    if isinstance(stmt, ast.Return):
        if stmt.value is None:
            return True
        try:
            return ast.unparse(stmt.value).strip() in _TRIVIAL_RETURNS
        except Exception:
            return False
    return False


def _class_is_protocol(cls: ast.ClassDef) -> bool:
    for base in cls.bases:
        name = (
            base.id
            if isinstance(base, ast.Name)
            else (base.attr if isinstance(base, ast.Attribute) else "")
        )
        if name == "Protocol":
            return True
    return False


def _is_501(node: ast.AST) -> bool:
    src = ""
    try:
        src = ast.unparse(node)
    except Exception:
        return False
    return "501" in src or "HTTP_501" in src or "NOT_IMPLEMENTED" in src.upper()


def check_source(path: Path, text: str) -> list[Violation]:
    out: list[Violation] = []
    # text-level rules (HOL002/HOL003) — skip this module's own rule definitions
    if path.name != "check_no_stubs.py":
        for i, line in enumerate(text.splitlines(), start=1):
            if _HOL003_RE.search(line):
                out.append(
                    Violation(
                        path,
                        i,
                        "HOL003",
                        "deferral marker (TODO: implement / not yet implemented / deferred to Rn)",
                    )
                )
            if _HOL002_RE.search(line):
                out.append(Violation(path, i, "HOL002", "stub marker ('_stub_' / 'stub_result')"))
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        return out + [Violation(path, e.lineno or 0, "HOL000", f"syntax error: {e.msg}")]

    protocol_classes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _class_is_protocol(node):
            for child in ast.walk(node):
                protocol_classes.add(id(child))

    for node in ast.walk(tree):
        if isinstance(node, ast.Raise):
            exc = node.exc
            exc_name = ""
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                exc_name = exc.func.id
            elif isinstance(exc, ast.Name):
                exc_name = exc.id
            if exc_name == "NotImplementedError":
                out.append(
                    Violation(
                        path, node.lineno, "HOL001", "raise NotImplementedError in non-test source"
                    )
                )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decos = _decorator_names(node)
            if {"abstractmethod", "overload"} & decos:
                continue
            if id(node) in protocol_classes:
                continue
            if _trivial_body(node):
                out.append(
                    Violation(
                        path,
                        node.lineno,
                        "HOL004",
                        f"stub body in '{node.name}' (only pass/.../trivial return)",
                    )
                )
        elif isinstance(node, ast.Return) and node.value is not None and _is_501(node.value):
            out.append(
                Violation(path, node.lineno, "HOL005", "route returns HTTP 501 / NOT_IMPLEMENTED")
            )
    return out


# ---- service resolution + status gating -------------------------------------------------------


def service_of(path: Path) -> str | None:
    parts = path.parts
    if "services" in parts:
        i = parts.index("services")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def is_non_test_src(path: Path) -> bool:
    parts = set(path.parts)
    return path.suffix == ".py" and "tests" not in parts and "test" not in path.stem.lower()


def load_status(status_path: Path) -> dict[str, dict]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return {}
    data = yaml.safe_load(status_path.read_text()) if status_path.exists() else {}
    return (data or {}).get("services", {})


def iter_py_files(paths: list[Path]):
    for p in paths:
        if p.is_dir():
            yield from sorted(p.rglob("*.py"))
        elif p.suffix == ".py":
            yield p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="No-hollow guardrail (HOL001-005).")
    ap.add_argument("paths", nargs="*", default=["services"])
    ap.add_argument("--status", default="tools/lint/service_status.yaml")
    ap.add_argument(
        "--all", action="store_true", help="enforce on ALL services regardless of claimed_done"
    )
    args = ap.parse_args(argv)

    status = load_status(Path(args.status))
    enforced_failures: list[Violation] = []
    info_count = 0

    for f in iter_py_files([Path(p) for p in args.paths]):
        if not is_non_test_src(f):
            continue
        viols = check_source(f, f.read_text(encoding="utf-8", errors="replace"))
        if not viols:
            continue
        svc = service_of(f)
        enforced = args.all or (svc is not None and bool(status.get(svc, {}).get("claimed_done")))
        if enforced:
            enforced_failures.extend(viols)
        else:
            info_count += len(viols)

    if enforced_failures:
        print("HOLLOWNESS — a claimed-done service contains stubs:", file=sys.stderr)
        for v in enforced_failures:
            print(f"  {v}", file=sys.stderr)
        return 1
    if info_count:
        print(
            f"check_no_stubs: {info_count} HOL marker(s) in not-yet-done services (informational)."
        )
    print("check_no_stubs: no enforced hollowness violations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
