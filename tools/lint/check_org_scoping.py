"""Static guardrails for organisation scoping (ADR-006), R0.5 story 0b.

Best-effort, heuristic lint rules (guardrails, not proofs). Postgres has a
runtime RLS backstop (ADR-012); Neo4j community does not, so for the Neo4j /
Redis dimensions these static checks are the **build-time anti-drift
compensating control** on the schema / DDL / cache-key seams — they catch the
shapes that would defeat tenant isolation at the schema boundary, but they are
NOT a runtime substitute for RLS (security-architect concurrence, ORA-41).

  ORG001 — ``organisation_id`` must not be read from an untrusted request body.
           It is resolved from the authenticated principal context (the
           ``packages/governance`` org-context), never trusted from the body.
           Flagged forms: dict-style extraction off a body/payload/request
           (``body["organisation_id"]``); an attribute read off an unambiguous
           HTTP-body name (``body.organisation_id`` / ``payload.organisation_id``);
           and an inbound Pydantic ``BaseModel`` request schema
           (a ``*Request``/``*Body``/``*Payload`` class) declaring
           ``organisation_id`` as a field. Deliberately NOT flagged: a plain
           domain value object (e.g. a frozen ``@dataclass``) that carries
           ``organisation_id`` to pass it *through* a seam, and attribute reads
           off ambiguous names (``request``/``req``/``data``) which routinely
           name such domain objects. Best-effort heuristic — the authoritative
           T1 control is the runtime org-context plus the organisation-isolation
           tests, not this linter (ORA-40 / security-architect ruling).
  ORG002 — a substrate storage model (a class with ``__tablename__``) must
           declare an ``organisation_id`` column.
  ORG003 — a Neo4j index/constraint DDL over an org-scoped label (or built over
           the org-scoped label loop, e.g. ``FOR (n:`{label}`)``) must include
           ``organisation_id``. Flags an org-scoped-label index/constraint whose
           ``ON`` / ``REQUIRE`` clause omits org; passes when org is present
           (as a literal ``organisation_id`` or an interpolated ``{ORG_PROPERTY}``).
  ORG004 — a Redis ``qcache`` key/pattern must carry ``organisation_id`` as its
           outer segment (``qcache:{organisation_id}:…``, ADR-006). Flags a
           ``qcache:{graph_id}:…``-style f-string whose first segment after the
           prefix is not the org scope. Two non-violations: a pure-wildcard
           namespace SCAN (``f"{_PREFIX}:*"``) is a maintenance glob, not a key
           write, and is exempt; a deliberately global key (rate-limit counter,
           health probe) must carry an explicit ``# org-scoping: global`` comment
           on its line rather than a silent bypass (security-architect
           precondition #2, ORA-41).
  ORG005 — a Neo4j vector/fulltext index DDL must include ``organisation_id``
           (a vector/fulltext index without org returns cross-org neighbours
           regardless of runtime filters). Flags such a ``CREATE VECTOR INDEX`` /
           ``CREATE FULLTEXT INDEX`` omitting org; passes when org is present.

The set of org-scoped labels is loaded from the single source of truth at
``packages/substrate/src/oraclous_substrate/schema/org_scoped_labels.yaml``
(ORA-51). Both the substrate (at module-import time) and this linter (at
lint time) derive from the same file, so structural drift is impossible —
adding a label to the YAML extends both the substrate's ``apply()`` coverage
and the ORG003 recognition set with no other code change. The YAML's shape is
validated in CI by ``tools.lint.check_org_scoped_labels_schema``.

Run:  uv run python -m tools.lint.check_org_scoping <path> [<path> ...]
Exits non-zero (1) if any violation is found; 0 otherwise.
"""

from __future__ import annotations

import ast
import functools
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ORG_NAMES = {"organisation_id", "organization_id"}
# Dict-style subscript (body["organisation_id"]) is checked against the full set;
# subscripting a domain value object to read its tenancy scope is not a real pattern.
BODY_SOURCES = {"body", "payload", "request", "req", "data"}
# Attribute reads (body.organisation_id) are checked only against unambiguous
# HTTP-body names. `request`/`req`/`data` routinely name domain objects that
# legitimately carry the tenancy scope through a seam, so flagging attribute reads
# off them is a false positive (ORA-40 / security-architect ruling).
ATTRIBUTE_BODY_SOURCES = {"body", "payload"}
REQUEST_MODEL_SUFFIXES = ("Request", "Body", "Payload")
PYDANTIC_BASES = {"BaseModel"}
SKIP_DIRS = {".venv", "venv", "__pycache__", "build", "dist", ".git"}

# --- ORA-41 / ORA-51: Neo4j label / Redis prefix / index DDL coverage -----------

# Canonical YAML path. Resolved from this module's filesystem location
# (tools/lint/check_org_scoping.py -> repo root -> packages/substrate/...). The
# linter NEVER imports the substrate at lint time; it reads the YAML directly,
# the same file the substrate consumes at module-import time.
_DEFAULT_ORG_SCOPED_LABELS_YAML = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "substrate"
    / "src"
    / "oraclous_substrate"
    / "schema"
    / "org_scoped_labels.yaml"
)


@functools.cache
def _load_org_scoped_labels(path: Path) -> tuple[str, ...]:
    """Read the labels list from the (canonical or override) YAML at lint time.

    Cached per-path so that ``check_paths`` scanning hundreds of files does not
    re-read the YAML hundreds of times. Tests using ``tmp_path`` get distinct
    cache entries; the canonical path is read once per process.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return tuple(data.get("labels") or ())


# Loop-variable idiom: a DDL f-string interpolating one of these names is building
# an index/constraint over the org-scoped label set (the canonical ``apply()`` loop
# in schema/neo4j.py iterates the substrate's label / relationship-type tuples).
LABEL_LOOP_VAR_NAMES = {
    "label",
    "labels",
    "lbl",
    "node_label",
    "rel",
    "rel_type",
    "relationship",
    "relationship_type",
}

# Word-tokens that denote the organisation scope in an interpolation or key segment.
_ORG_TOKENS = {
    "org",
    "orgs",
    "organisation",
    "organization",
    "organisationid",
    "organizationid",
    "orgid",
}

_CYPHER_CREATE_RE = re.compile(r"\bCREATE\b", re.IGNORECASE)
_CYPHER_INDEX_OR_CONSTRAINT_RE = re.compile(r"\b(INDEX|CONSTRAINT)\b", re.IGNORECASE)
_CYPHER_VECTOR_RE = re.compile(r"\bVECTOR\s+INDEX\b", re.IGNORECASE)
_CYPHER_FULLTEXT_RE = re.compile(r"\bFULLTEXT\s+INDEX\b", re.IGNORECASE)

# A deliberately-global Redis key opts out of ORG004 with this comment on its line.
GLOBAL_OPT_OUT_MARKER = "org-scoping: global"

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")


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


def _name_is_org_ish(name: str) -> bool:
    """Does this identifier/string denote the organisation scope?

    Matches ``organisation_id``/``organization_id`` substrings and any token of
    ``org``/``organisation``/``organization`` (covers ``org``, ``org_id``,
    ``ORG_A``, ``ORG_PROPERTY``, ``SEED_ORGANISATION_ID``). Does NOT match
    ``graph_id``, ``graph``, or unrelated names — so org-less drift still flags.
    """
    low = name.lower()
    if "organisation_id" in low or "organization_id" in low:
        return True
    return any(tok in _ORG_TOKENS for tok in _WORD_SPLIT_RE.split(low) if tok)


def _is_org_expr(node: ast.expr) -> bool:
    """Best-effort: does this expression evaluate to the organisation scope?"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _name_is_org_ish(node.value)
    if isinstance(node, ast.Name):
        return _name_is_org_ish(node.id)
    if isinstance(node, ast.Attribute):
        return _name_is_org_ish(node.attr)
    return False


def _is_pydantic_model(node: ast.ClassDef) -> bool:
    """Best-effort: does this class directly inherit a Pydantic ``BaseModel``?

    Domain value objects (frozen ``@dataclass``es) carrying ``organisation_id``
    through a seam are not inbound request schemas and must not be flagged. A
    model reached only via a project-local base class is not detected — accepted
    as a best-effort limit.
    """
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in PYDANTIC_BASES:
            return True
        if isinstance(base, ast.Attribute) and base.attr in PYDANTIC_BASES:
            return True
    return False


# --- string decomposition helpers (str Constant or f-string) --------------------


def _string_parts(node: ast.expr) -> list[tuple[str, object]] | None:
    """Decompose a str ``Constant`` or f-string into ordered parts.

    Returns a list of ``("lit", text)`` and ``("expr", ast_node)`` parts, or
    ``None`` if ``node`` is not a string node.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [("lit", node.value)]
    if isinstance(node, ast.JoinedStr):
        parts: list[tuple[str, object]] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(("lit", value.value))
            elif isinstance(value, ast.FormattedValue):
                parts.append(("expr", value.value))
        return parts
    return None


def _static_text(parts: list[tuple[str, object]]) -> str:
    return "".join(text for kind, text in parts if kind == "lit")  # type: ignore[misc]


def _exprs(parts: list[tuple[str, object]]) -> list[ast.expr]:
    return [node for kind, node in parts if kind == "expr"]  # type: ignore[misc]


# --- Neo4j DDL (ORG003 / ORG005) ------------------------------------------------


def _is_cypher_index_or_constraint(text: str) -> bool:
    return bool(_CYPHER_CREATE_RE.search(text) and _CYPHER_INDEX_OR_CONSTRAINT_RE.search(text))


def _ddl_has_org(parts: list[tuple[str, object]]) -> bool:
    text = _static_text(parts).lower()
    if "organisation_id" in text or "organization_id" in text:
        return True
    return any(_is_org_expr(expr) for expr in _exprs(parts))


def _ddl_targets_org_scoped_label(parts: list[tuple[str, object]], labels: tuple[str, ...]) -> bool:
    text = _static_text(parts)
    for label in labels:
        # The label appears as a literal Cypher token: `Label`, :Label, (n:Label).
        if f"`{label}`" in text or re.search(r"[`:\s(]" + re.escape(label) + r"[`)\s,]", text):
            return True
    # The label is interpolated from the org-scoped label loop (canonical apply()).
    return any(
        isinstance(expr, ast.Name) and expr.id in LABEL_LOOP_VAR_NAMES for expr in _exprs(parts)
    )


# --- Redis qcache key (ORG004) --------------------------------------------------


def _split_segments(parts: list[tuple[str, object]]) -> list[list[tuple[str, object]]]:
    """Split f-string parts into ``:``-delimited segments (atoms per segment)."""
    segments: list[list[tuple[str, object]]] = [[]]
    for kind, val in parts:
        if kind == "lit":
            pieces = str(val).split(":")
            for i, piece in enumerate(pieces):
                if i > 0:
                    segments.append([])
                if piece:
                    segments[-1].append(("lit", piece))
        else:
            segments[-1].append(("expr", val))
    return segments


def _segment_is_qcache_prefix(seg: list[tuple[str, object]], prefix_names: set[str]) -> bool:
    for kind, val in seg:
        if kind == "lit" and "qcache" in str(val).lower():
            return True
        if kind == "expr" and isinstance(val, ast.Name) and val.id in prefix_names:
            return True
    return False


def _segment_is_pure_wildcard(seg: list[tuple[str, object]]) -> bool:
    return len(seg) == 1 and seg[0][0] == "lit" and str(seg[0][1]).strip() == "*"


def _segment_has_org(seg: list[tuple[str, object]]) -> bool:
    for kind, val in seg:
        if kind == "lit" and _name_is_org_ish(str(val)):
            return True
        if kind == "expr" and isinstance(val, ast.expr) and _is_org_expr(val):
            return True
    return False


# --- module pre-passes ----------------------------------------------------------


def _collect_qcache_prefix_names(tree: ast.AST) -> set[str]:
    """Module-level names bound to the constant ``"qcache"`` (e.g. ``_PREFIX``)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        value: object = None
        targets: list[str] = []
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            value = node.value.value
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.target, ast.Name)
        ):
            value = node.value.value
            targets = [node.target.id]
        if isinstance(value, str) and value.lower() == "qcache":
            names.update(targets)
    return names


def _collect_docstring_ids(tree: ast.AST) -> set[int]:
    """Object ids of docstring Constants (module/class/function) — not executable DDL."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


class _Visitor(ast.NodeVisitor):
    def __init__(
        self,
        path: str,
        source: str,
        qcache_prefix_names: set[str],
        docstring_ids: set[int],
        org_scoped_labels: tuple[str, ...],
    ) -> None:
        self.path = path
        self.source_lines = source.splitlines()
        self.qcache_prefix_names = qcache_prefix_names
        self.docstring_ids = docstring_ids
        self.org_scoped_labels = org_scoped_labels
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
            and node.value.id in ATTRIBUTE_BODY_SOURCES
            and node.attr in ORG_NAMES
        ):
            self._flag_body_read(node.lineno)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name.endswith(REQUEST_MODEL_SUFFIXES) and _is_pydantic_model(node):
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

    def visit_Constant(self, node: ast.Constant) -> None:
        # Standalone string literal (e.g. a DDL assigned to a variable). f-string
        # literal fragments are handled by visit_JoinedStr, never reached here.
        if isinstance(node.value, str) and id(node) not in self.docstring_ids:
            self._check_ddl(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self._check_ddl(node)
        self._check_redis_key(node)
        # Descend only into interpolated expressions (to catch e.g. ORG001 inside
        # `{...}`), never into the literal Constant fragments, so a DDL/key split
        # across fragments is evaluated once, as a whole, by the checks above.
        for value in node.values:
            if isinstance(value, ast.FormattedValue):
                self.visit(value.value)

    def _check_ddl(self, node: ast.expr) -> None:
        parts = _string_parts(node)
        if parts is None:
            return
        text = _static_text(parts)
        if not _is_cypher_index_or_constraint(text):
            return
        is_vector = bool(_CYPHER_VECTOR_RE.search(text))
        is_fulltext = bool(_CYPHER_FULLTEXT_RE.search(text))
        if (is_vector or is_fulltext) and not _ddl_has_org(parts):
            kind = "vector" if is_vector else "fulltext"
            self.violations.append(
                Violation(
                    "ORG005",
                    self.path,
                    node.lineno,
                    f"{kind} index DDL omits organisation_id "
                    "(ADR-006; cross-org results, no Neo4j RLS backstop)",
                )
            )
            return
        if (
            not (is_vector or is_fulltext)
            and _ddl_targets_org_scoped_label(parts, self.org_scoped_labels)
            and not _ddl_has_org(parts)
        ):
            self.violations.append(
                Violation(
                    "ORG003",
                    self.path,
                    node.lineno,
                    "Neo4j org-scoped label index/constraint DDL omits organisation_id (ADR-006)",
                )
            )

    def _check_redis_key(self, node: ast.JoinedStr) -> None:
        parts = _string_parts(node)
        if parts is None:
            return
        segments = _split_segments(parts)
        if len(segments) < 2 or not _segment_is_qcache_prefix(
            segments[0], self.qcache_prefix_names
        ):
            return
        first = segments[1]
        # A pure-wildcard namespace SCAN (f"{_PREFIX}:*") is a maintenance glob,
        # not a per-tenant key write — structurally cannot scope to one org, exempt.
        if _segment_is_pure_wildcard(first) or _segment_has_org(first):
            return
        if self._has_global_marker(node):
            return
        self.violations.append(
            Violation(
                "ORG004",
                self.path,
                node.lineno,
                "Redis qcache key/pattern lacks the organisation_id outer segment (ADR-006); "
                f"annotate a deliberately global key with `# {GLOBAL_OPT_OUT_MARKER}`",
            )
        )

    def _has_global_marker(self, node: ast.expr) -> bool:
        start = node.lineno - 1
        end = getattr(node, "end_lineno", None) or node.lineno
        return any(GLOBAL_OPT_OUT_MARKER in line.lower() for line in self.source_lines[start:end])


def check_source(
    source: str,
    path: str = "<string>",
    *,
    org_scoped_labels_yaml: Path | None = None,
) -> list[Violation]:
    """Run the guardrails over a single source string.

    ``org_scoped_labels_yaml`` overrides the canonical YAML lookup; when ``None``
    the canonical ``packages/substrate/.../schema/org_scoped_labels.yaml`` is
    used. The override exists so tests can exercise the YAML-driven recognition
    without globally mutating the canonical file.
    """
    tree = ast.parse(source, filename=path)
    yaml_path = org_scoped_labels_yaml or _DEFAULT_ORG_SCOPED_LABELS_YAML
    labels = _load_org_scoped_labels(yaml_path)
    visitor = _Visitor(
        path,
        source,
        _collect_qcache_prefix_names(tree),
        _collect_docstring_ids(tree),
        labels,
    )
    visitor.visit(tree)
    return visitor.violations


def check_paths(
    paths: list[str],
    *,
    org_scoped_labels_yaml: Path | None = None,
) -> list[Violation]:
    out: list[Violation] = []
    for raw in paths:
        root = Path(raw)
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for f in files:
            if any(part in SKIP_DIRS for part in f.parts):
                continue
            out.extend(
                check_source(
                    f.read_text(encoding="utf-8"),
                    str(f),
                    org_scoped_labels_yaml=org_scoped_labels_yaml,
                )
            )
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
