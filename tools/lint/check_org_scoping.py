"""Static guardrails for organisation scoping (ADR-006), R0.5 story 0b.

Two best-effort, heuristic lint rules (guardrails, not proofs):

  ORG001 — ``organisation_id`` must not be read from a request body / payload.
           It is resolved from the authenticated principal context (the
           ``packages/governance`` org-context), never trusted from the body.
  ORG002 — a substrate storage model (a class with ``__tablename__``) must
           declare an ``organisation_id`` column.

Run:  uv run python -m tools.lint.check_org_scoping <path> [<path> ...]
Exits non-zero (1) if any violation is found; 0 otherwise.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ORG_NAMES = {"organisation_id", "organization_id"}
BODY_SOURCES = {"body", "payload", "request", "req", "data"}
REQUEST_MODEL_SUFFIXES = ("Request", "Body", "Payload")
SKIP_DIRS = {".venv", "venv", "__pycache__", "build", "dist", ".git"}


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def _is_org_key(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value in ORG_NAMES


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.violations: list[Violation] = []

    def _flag_body_read(self, line: int) -> None:
        self.violations.append(
            Violation(
                "ORG001",
                self.path,
                line,
                "organisation_id must come from authenticated context, not the request body",
            )
        )

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if (
            isinstance(node.value, ast.Name)
            and node.value.id in BODY_SOURCES
            and _is_org_key(node.slice)
        ):
            self._flag_body_read(node.lineno)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            isinstance(node.value, ast.Name)
            and node.value.id in BODY_SOURCES
            and node.attr in ORG_NAMES
        ):
            self._flag_body_read(node.lineno)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name.endswith(REQUEST_MODEL_SUFFIXES):
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and stmt.target.id in ORG_NAMES
                ):
                    self.violations.append(
                        Violation(
                            "ORG001",
                            self.path,
                            stmt.lineno,
                            f"request model '{node.name}' takes organisation_id as input",
                        )
                    )
        self._check_substrate_model(node)
        self.generic_visit(node)

    def _check_substrate_model(self, node: ast.ClassDef) -> None:
        has_tablename = any(
            isinstance(s, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__tablename__" for t in s.targets)
            for s in node.body
        )
        if not has_tablename:
            return
        declares_org = False
        for s in node.body:
            targets: list[ast.expr] = []
            if isinstance(s, ast.Assign):
                targets = list(s.targets)
            elif isinstance(s, ast.AnnAssign):
                targets = [s.target]
            if any(isinstance(t, ast.Name) and t.id in ORG_NAMES for t in targets):
                declares_org = True
        if not declares_org:
            self.violations.append(
                Violation(
                    "ORG002",
                    self.path,
                    node.lineno,
                    f"storage model '{node.name}' declares no organisation_id column (ADR-006)",
                )
            )


def check_source(source: str, path: str = "<string>") -> list[Violation]:
    visitor = _Visitor(path)
    visitor.visit(ast.parse(source, filename=path))
    return visitor.violations


def check_paths(paths: list[str]) -> list[Violation]:
    out: list[Violation] = []
    for raw in paths:
        root = Path(raw)
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for f in files:
            if any(part in SKIP_DIRS for part in f.parts):
                continue
            out.extend(check_source(f.read_text(encoding="utf-8"), str(f)))
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    paths = args or ["packages", "services"]
    violations = check_paths(paths)
    for v in violations:
        print(v)
    if violations:
        print(f"\n{len(violations)} organisation-scoping violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
