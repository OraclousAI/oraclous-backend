"""Static guardrail: no publicly-known default for a security secret (WP-1, T6 / ADR-008).

A baked-in default for a security-material field (the internal service key, the JWT secret, an
OAuth/encryption key, any ``*_secret`` / ``*_api_key``) is a silent production footgun: a deploy
that forgets to inject the real value boots anyway with a key an attacker can read off the tree.
The correct pattern is fail-closed resolution (``oraclous_governance.require_secret`` / a no-default
pydantic field), which raises in prod and falls back to a dev default only when ``RUN_MODE!=prod``.

This linter AST-scans every ``services/*/src/**/core/config.py`` and ``**/core/encryption.py`` and
denies, for a **secret-named** target, either of:

  FCS001 — a string-literal default assignment to a secret field, e.g.
           ``INTERNAL_SERVICE_KEY: str = "dev-internal-key"`` or ``jwt_secret = "change-me"``.
           A field with no string-literal default (``INTERNAL_SERVICE_KEY: str``, ``... = ""``,
           ``... = None``) is fine — pydantic/the dataclass then has no baked-in key, and
           require_secret supplies the gated dev fallback.
  FCS002 — an ``os.environ.get(name, "<literal>")`` / ``os.getenv(name, "<literal>")`` with a
           non-empty string default for a secret-named ``name``, e.g.
           ``os.environ.get("JWT_SECRET", "change-me-in-production")``. This is the raw form WP-1
           replaced with require_secret. (A two-arg get with ``""``/``None`` default is allowed.)

Secret field/name detection (case-insensitive): the explicit set {jwt_secret, internal_service_key,
oauth_enc_key, encryption_key} plus any name ending in ``_secret`` or ``_api_key``. The dev-default
*constants* that feed require_secret (any leading-underscore ``_DEV*`` private name, e.g.
``_DEV_KEY`` / ``_DEV_JWT_SECRET``) are NOT secret fields — they are the gated fallback values
themselves and are exempt, so the fixed config still passes.

Run:  uv run python -m tools.lint.check_failclosed_secrets [<path> ...]
Defaults to scanning ``services``. Exits non-zero (1) if any violation is found; 0 otherwise.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".venv", "venv", "__pycache__", "build", "dist", ".git"}

# Files whose security-material defaults this guardrail polices. A path qualifies when it ends in
# one of these tails under a service ``src`` tree (or anywhere for encryption.py).
_CONFIG_TAIL = ("core", "config.py")
_ENCRYPTION_NAME = "encryption.py"

# Explicit secret field names (case-insensitive) plus suffix rules.
_EXPLICIT_SECRET_NAMES = {
    "jwt_secret",
    "internal_service_key",
    "oauth_enc_key",
    "encryption_key",
}
_SECRET_SUFFIXES = ("_secret", "_api_key")

# Names that are dev-default *fallback values* (gated by require_secret), not secret fields. A
# leading-underscore ``_DEV*`` constant is the permitted dev fallback and is never itself a secret.
_DEV_FALLBACK_PREFIXES = ("_DEV",)


def _is_secret_name(name: str) -> bool:
    low = name.lower()
    if low in _EXPLICIT_SECRET_NAMES:
        return True
    return low.endswith(_SECRET_SUFFIXES)


def _is_dev_fallback_name(name: str) -> bool:
    return any(name.startswith(p) for p in _DEV_FALLBACK_PREFIXES)


def _is_nonempty_str_literal(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value != ""


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


def _is_environ_get(node: ast.Call) -> str | None:
    """If ``node`` is ``os.environ.get(...)`` / ``os.getenv(...)``, return the looked-up name."""
    func = node.func
    is_getter = False
    if isinstance(func, ast.Attribute):
        # os.getenv(...) | os.environ.get(...)
        if func.attr == "getenv":
            is_getter = True
        elif (
            func.attr == "get"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "environ"
        ):
            is_getter = True
        elif func.attr == "get" and isinstance(func.value, ast.Name) and func.value.id == "environ":
            is_getter = True
    if not is_getter or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.violations: list[Violation] = []

    def _target_names(self, stmt: ast.Assign | ast.AnnAssign) -> list[str]:
        names: list[str] = []
        targets: list[ast.expr] = (
            list(stmt.targets) if isinstance(stmt, ast.Assign) else [stmt.target]
        )
        for t in targets:
            if isinstance(t, ast.Name):
                names.append(t.id)
            elif isinstance(t, ast.Attribute):
                names.append(t.attr)
        return names

    def _check_literal_default(self, stmt: ast.Assign | ast.AnnAssign) -> None:
        names = self._target_names(stmt)
        secret_targets = [n for n in names if _is_secret_name(n) and not _is_dev_fallback_name(n)]
        if not secret_targets:
            return
        if _is_nonempty_str_literal(stmt.value):
            self.violations.append(
                Violation(
                    "FCS001",
                    self.path,
                    stmt.lineno,
                    f"secret field '{secret_targets[0]}' has a string-literal default; resolve it "
                    "fail-closed via oraclous_governance.require_secret (no baked-in key in prod).",
                )
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_literal_default(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_literal_default(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        env_name = _is_environ_get(node)
        if (
            env_name is not None
            and _is_secret_name(env_name)
            and len(node.args) >= 2
            and _is_nonempty_str_literal(node.args[1])
        ):
            self.violations.append(
                Violation(
                    "FCS002",
                    self.path,
                    node.lineno,
                    f"os.environ.get('{env_name}', '<literal>') bakes in a secret default; use "
                    "oraclous_governance.require_secret (fails closed in prod).",
                )
            )
        self.generic_visit(node)


def check_source(source: str, path: str = "<string>") -> list[Violation]:
    tree = ast.parse(source, filename=path)
    visitor = _Visitor(path)
    visitor.visit(tree)
    return visitor.violations


def _is_policed_file(f: Path) -> bool:
    if f.name == _ENCRYPTION_NAME:
        return True
    parts = f.parts
    # ends in .../core/config.py
    return len(parts) >= 2 and parts[-2] == _CONFIG_TAIL[0] and parts[-1] == _CONFIG_TAIL[1]


def check_paths(paths: list[str]) -> list[Violation]:
    out: list[Violation] = []
    for raw in paths:
        root = Path(raw)
        candidates = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for f in candidates:
            if any(part in SKIP_DIRS for part in f.parts):
                continue
            if not _is_policed_file(f):
                continue
            out.extend(check_source(f.read_text(encoding="utf-8"), str(f)))
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    paths = args or ["services"]
    violations = check_paths(paths)
    for v in violations:
        print(v)
    if violations:
        print(f"\n{len(violations)} fail-closed-secret violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
