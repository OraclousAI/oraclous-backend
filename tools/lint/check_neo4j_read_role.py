"""Static guardrail: knowledge-retriever-service must use the read-role Neo4j
credential and must contain no write Cypher (ORAA-58 / T6).

Acceptance criterion AC2: "No write Cypher (CREATE, MERGE, SET, DELETE) in
retriever service."

The knowledge-retriever-service (read path) connects to Neo4j via the
dedicated ``krs_reader`` role.  The three env vars that carry that credential
are:

    KRS_NEO4J_URI       bolt URI for the knowledge-retriever-service
    KRS_NEO4J_USER      krs_reader (reader role)
    KRS_NEO4J_PASSWORD  injected from K8s secret in production; local dev default

This check enforces three families of violation:

  NEO4J_KRS001 — a string literal inside
                 ``services/knowledge-retriever-service/`` contains a Cypher
                 write keyword (CREATE, MERGE, DELETE, REMOVE, or SET used as
                 a property setter: ``SET <ident>.<prop> =``).
                 The retriever service is read-only; write Cypher indicates
                 either a misplaced implementation or a Cypher-injection
                 vector (T6).

  NEO4J_KRS002 — direct use of the generic ``NEO4J_URI`` / ``NEO4J_USER`` /
                 ``NEO4J_PASSWORD`` / ``NEO4J_AUTH`` admin env vars inside
                 ``services/knowledge-retriever-service/`` source files.
                 Using the admin credential bypasses least-privilege (T6) and
                 could grant write access via the admin account.

  NEO4J_KRS003 — direct use of write-role env vars (``KGS_NEO4J_URI`` /
                 ``KGS_NEO4J_USER`` / ``KGS_NEO4J_PASSWORD``) inside
                 ``services/knowledge-retriever-service/`` source files.
                 The KRS must use KRS_NEO4J_* credentials, not the write-path
                 KGS credentials.

  NEO4J_KRS004 — a hardcoded ``bolt://`` or ``neo4j://`` URI literal inside
                 ``services/knowledge-retriever-service/`` source files.
                 Connection URIs must come from ``KRS_NEO4J_URI``; hardcoding
                 prevents operators from rotating or separating credentials.

Best-effort static analysis — it catches the obvious shapes.  The definitive
runtime enforcement is the Docker / K8s environment injection: the KRS
container only receives ``KRS_NEO4J_*`` env vars, not ``NEO4J_*`` or
``KGS_NEO4J_*``, and the ``krs_reader`` account has no write privileges in
Neo4j.

Run:  uv run python -m tools.lint.check_neo4j_read_role [<path>]
      <path> defaults to services/knowledge-retriever-service
Exits non-zero (1) if any violation is found; 0 otherwise.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Generic admin env vars that the KRS must NOT use.
_ADMIN_ENV_VARS: frozenset[str] = frozenset(
    {"NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "NEO4J_AUTH"}
)

# Write-path (KGS) env vars that the KRS must NOT use.
_KGS_ENV_VARS: frozenset[str] = frozenset({"KGS_NEO4J_URI", "KGS_NEO4J_USER", "KGS_NEO4J_PASSWORD"})

# Pattern for hardcoded Neo4j bolt/neo4j URI schemes.
_BOLT_URI_RE = re.compile(r"\b(bolt|neo4j)(\+s)?://", re.IGNORECASE)

# Cypher write keywords that must not appear as standalone uppercase words in
# string literals inside the KRS.  Matching is case-sensitive for the bare
# keywords (Cypher convention is uppercase; lowercase "create" / "delete" are
# common in English prose and Python method names — matching them case-
# insensitively produces too many false positives in docstrings).
# The `SET` branch uses a more specific property-setter pattern so it stays
# case-insensitive without triggering on ``set()`` calls or prose.
_CYPHER_WRITE_KEYWORDS_RE = re.compile(
    r"\b(CREATE|MERGE|DELETE|REMOVE)\b"
    r"|"
    r"\bSET\s+\w+\.\w+\s*=",
)

SKIP_DIRS = frozenset({".venv", "venv", "__pycache__", "build", "dist", ".git", "tests"})

_DEFAULT_KRS_PATH = Path(__file__).resolve().parents[2] / "services" / "knowledge-retriever-service"


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
    # NEO4J_KRS001 — Cypher write keywords in string literals
    # ------------------------------------------------------------------

    def _check_cypher_write(self, value: str, line: int) -> None:
        match = _CYPHER_WRITE_KEYWORDS_RE.search(value)
        if match:
            keyword = match.group(0).split()[0].upper()
            self.violations.append(
                Violation(
                    "NEO4J_KRS001",
                    self.path,
                    line,
                    f"Cypher write keyword {keyword!r} in string literal inside "
                    "knowledge-retriever-service; KRS is read-only (ORAA-58 / T6)",
                )
            )

    # ------------------------------------------------------------------
    # NEO4J_KRS002 — generic admin env var access inside KRS
    # ------------------------------------------------------------------

    def _check_admin_env_var(self, name: str, line: int) -> None:
        if name in _ADMIN_ENV_VARS:
            self.violations.append(
                Violation(
                    "NEO4J_KRS002",
                    self.path,
                    line,
                    f"admin env var {name!r} used in knowledge-retriever-service; "
                    "use KRS_NEO4J_* credentials instead (ORAA-58 / T6)",
                )
            )

    # ------------------------------------------------------------------
    # NEO4J_KRS003 — KGS write-role env var access inside KRS
    # ------------------------------------------------------------------

    def _check_kgs_env_var(self, name: str, line: int) -> None:
        if name in _KGS_ENV_VARS:
            self.violations.append(
                Violation(
                    "NEO4J_KRS003",
                    self.path,
                    line,
                    f"write-role env var {name!r} used in knowledge-retriever-service; "
                    "KRS must use KRS_NEO4J_* credentials, not KGS_NEO4J_* (ORAA-58 / T6)",
                )
            )

    def _check_env_var(self, name: str, line: int) -> None:
        self._check_admin_env_var(name, line)
        self._check_kgs_env_var(name, line)

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
    # NEO4J_KRS001 + NEO4J_KRS004 — string constant inspection
    # ------------------------------------------------------------------

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            # NEO4J_KRS001: write Cypher in string literal
            self._check_cypher_write(node.value, node.lineno)
            # NEO4J_KRS004: hardcoded bolt URI
            if _BOLT_URI_RE.search(node.value):
                self.violations.append(
                    Violation(
                        "NEO4J_KRS004",
                        self.path,
                        node.lineno,
                        f"hardcoded Neo4j URI {node.value!r} in knowledge-retriever-service; "
                        "connection URI must come from KRS_NEO4J_URI (ORAA-58)",
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
    paths = args if args else [str(_DEFAULT_KRS_PATH)]
    violations = check_paths(paths)
    for v in violations:
        print(v)
    if violations:
        print(f"\n{len(violations)} Neo4j read-role bypass violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
