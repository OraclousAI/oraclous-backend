"""Static guardrail: knowledge-graph-service must use the write-role Neo4j credential
(T6).

Acceptance criterion AC2: "No code path bypasses write-capable connection."

The knowledge-graph-service (write path) connects to Neo4j via the dedicated
``kgs_writer`` role.  The three env vars that carry that credential are:

    KGS_NEO4J_URI       bolt URI for the knowledge-graph-service
    KGS_NEO4J_USER      kgs_writer (publisher role)
    KGS_NEO4J_PASSWORD  injected from K8s secret in production; local dev default

Bypass patterns that this check flags:

  NEO4J001 — any direct use of the generic ``NEO4J_URI`` / ``NEO4J_USER`` /
             ``NEO4J_PASSWORD`` env vars (the admin credential) inside
             ``services/knowledge-graph-service/`` source files.
             Using the admin credential as the KGS connection violates T6
             (principle of least privilege) — a compromised KGS token would
             then have full admin access to Neo4j.

  NEO4J002 — a hardcoded ``bolt://`` or ``neo4j://`` URI literal inside
             ``services/knowledge-graph-service/`` source files.
             Connection URIs must come from ``KGS_NEO4J_URI``; hardcoding a
             URI prevents the operator from rotating or separating credentials.

The check is intentionally narrow: it only applies to
``services/knowledge-graph-service/`` (the write path).  Other services have
their own credential conventions enforced by their respective guardrails.

Best-effort static analysis — it catches the obvious bypass shapes.  The
definitive runtime enforcement is the Docker / K8s environment injection:
the KGS container only receives ``KGS_NEO4J_*`` env vars, not ``NEO4J_*``.

Run:  uv run python -m tools.lint.check_neo4j_write_role [<path>]
      <path> defaults to services/knowledge-graph-service
Exits non-zero (1) if any violation is found; 0 otherwise.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Generic admin env var names that the KGS must NOT use.
_ADMIN_ENV_VARS: frozenset[str] = frozenset(
    {"NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "NEO4J_AUTH"}
)

# Correct KGS-scoped env var prefix.
_KGS_PREFIX = "KGS_NEO4J_"

# Pattern for hardcoded Neo4j bolt/neo4j URI schemes.
_BOLT_URI_RE = re.compile(r"\b(bolt|neo4j)(\+s)?://", re.IGNORECASE)

SKIP_DIRS = frozenset({".venv", "venv", "__pycache__", "build", "dist", ".git"})

_DEFAULT_KGS_PATH = Path(__file__).resolve().parents[2] / "services" / "knowledge-graph-service"


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def _extract_env_var_name(node: ast.expr) -> str | None:
    """Return the string value if node is a str Constant, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.violations: list[Violation] = []

    # ------------------------------------------------------------------
    # NEO4J001 — generic admin env var access inside KGS
    # ------------------------------------------------------------------

    def _check_env_var(self, name: str, line: int) -> None:
        if name in _ADMIN_ENV_VARS:
            self.violations.append(
                Violation(
                    "NEO4J001",
                    self.path,
                    line,
                    f"admin env var {name!r} used in knowledge-graph-service; "
                    f"use KGS_NEO4J_* credentials instead (T6)",
                )
            )

    def visit_Call(self, node: ast.Call) -> None:
        # os.environ["NEO4J_URI"], os.environ.get("NEO4J_URI"), os.getenv("NEO4J_URI")
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in {"get", "getenv"} and node.args:
                name = _extract_env_var_name(node.args[0])
                if name:
                    self._check_env_var(name, node.lineno)
        elif isinstance(func, ast.Name) and func.id == "getenv":
            if node.args:
                name = _extract_env_var_name(node.args[0])
                if name:
                    self._check_env_var(name, node.lineno)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # os.environ["NEO4J_URI"]
        if isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
            name = _extract_env_var_name(node.slice)
            if name:
                self._check_env_var(name, node.lineno)
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # NEO4J002 — hardcoded bolt:// / neo4j:// URIs
    # ------------------------------------------------------------------

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _BOLT_URI_RE.search(node.value):
            self.violations.append(
                Violation(
                    "NEO4J002",
                    self.path,
                    node.lineno,
                    f"hardcoded Neo4j URI {node.value!r} in knowledge-graph-service; "
                    "connection URI must come from KGS_NEO4J_URI",
                )
            )
        self.generic_visit(node)


def check_source(source: str, path: str = "<string>") -> list[Violation]:
    tree = ast.parse(source, filename=path)
    visitor = _Visitor(path)
    visitor.visit(tree)
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
    paths = args if args else [str(_DEFAULT_KGS_PATH)]
    violations = check_paths(paths)
    for v in violations:
        print(v)
    if violations:
        print(f"\n{len(violations)} Neo4j write-role bypass violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
