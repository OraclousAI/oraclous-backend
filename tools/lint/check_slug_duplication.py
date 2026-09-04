"""Slug-duplication guardrail (#731) — a new hand-written twin of the shared plain-text slug
primitive fails CI instead of drifting again.

#731 collapsed five hand-written copies of ``oraclous_ohm._slug.basic_slug`` (plus one more
undocumented copy) onto that one shared definition — the same failure class #694 already fixed
once for ``tool_slug``, one rung down. That prior duplication was not cosmetic: two on-ramps
disagreed about what a tool was CALLED because neither could see the other's answer, and it cost a
production incident (team run ``fe548aac``). This guardrail is the static backstop so the next
"just inline it, it's three lines" copy is caught at review time, not the next time two readers of
the same name quietly disagree.

DETECTION IS AST, FUNCTION-SCOPED, NEVER TEXT. A function is flagged (SLUG001) only when its OWN
body (not a nested function's, not a sibling's) BOTH:

  1. calls ``.lower()`` somewhere, AND
  2. performs a regex substitution whose pattern is a known non-alphanumeric character class —
     either inline (``re.sub(r"[^a-z0-9]+", ...)``) or via a name bound, at module level, to
     ``re.compile(r"[^a-z0-9]+")`` (``_COMPILED.sub(...)``).

The ``AND`` is what gives this zero false positives. A function that only lower-cases
(``policy._registry_of`` — a deliberate case-fold, not a slug normaliser; hyphenating it would fail
OPEN a governance allow-list glob) or only calls an ALREADY-shared slug function
(``registry_client._ref_slug`` — a caller, not a new copy) must never trip it. Scoping to a single
function's own AST subtree (not the whole file) matters because real files in this repository put
a ``.lower()`` call and a non-alphanumeric-class ``.sub()`` call in two DIFFERENT functions of one
module (``sql_connector.py``, ``tool_schemas.py``, ``validation_passthrough.py``) — none of which
is a slug duplicate; a file-scoped scan would wrongly flag all three.

The scan walks the whole repository, pruning as it descends: any ``tests`` directory (at any
depth), the top-level ``tools/``, ``scripts/`` and read-only ``legacy-reference/`` trees, and
build/VCS/venv noise (hidden dirs, ``__pycache__`` and friends) are never entered. What is left is
``packages/*/src/**`` and ``services/*/src/**`` — the only source this guardrail cares about —
without hard-coding that layout, so a differently-shaped tree (a test fixture) is scanned the same
way.

WHAT THIS CANNOT CATCH (by design — read the behaviour tests for these, not this module):

  * An equivalent implementation written a DIFFERENT way — a comprehension, ``str.translate``, a
    third-party slugify library. The AST shape this checks for is narrow on purpose (zero false
    positives over broad recall).
  * Drift INSIDE an allow-listed copy (the canonical definition site itself, or any future
    allow-listed exception) — the allow-list only says "this shape is known and reviewed", not
    "this body is byte-identical to the primitive".
  * A call site repointed at ``tool_slug`` instead of ``basic_slug``. Both are "the shared
    function" syntactically — a plain caller of either looks identical to this static check (a
    function calling a name, no ``.lower()``/``.sub()`` of its own) — so no AST rule can tell them
    apart. Only the behaviour tests (``test_shared_tool_slug.py``'s
    ``test_every_plain_reader_agrees_with_the_shared_primitive`` and its neighbours) catch that.
  * A copy SPLIT ACROSS a function and a nested helper — the ``.lower()`` inside an inner function,
    the substitution in the outer body. Each half is judged on its own body, and neither half alone
    is the flagged shape. This is the deliberate cost of judging a function on its own body rather
    than its whole subtree: descending into nested definitions instead would flag two innocent
    functions that merely happen to contain a ``.lower()`` and an unrelated substitution, and
    attribute the finding to the wrong name — sending the reader to a place where nothing is wrong.
    A false alarm on real code is worse here than a miss on a shape nobody writes.

Violations:
  SLUG001 — a function matches the hand-written-copy shape and is not allow-listed.
  SLUG002 — a malformed manifest entry (missing ``path`` or ``function``); fails loud rather than
            silently passing everything or silently allow-listing a whole file.
  SLUG003 — a stale allow-list entry: it names a file, function, or shape that no longer exists /
            no longer matches SLUG001 — dead weight a reviewer can no longer verify against real
            code, so the list self-cleans instead of silently protecting nothing.

Run:  uv run python -m tools.lint.check_slug_duplication [--manifest <path>]
Exits non-zero (1) on any violation; 0 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_MANIFEST = Path(__file__).resolve().parent / "slug_duplication.yaml"

# Top-level directories never scanned, regardless of depth this rule is applied at (CLAUDE.md §12:
# legacy-reference is read-only; tools/ and scripts/ are operator code, not service/package code).
_EXCLUDED_TOP_LEVEL_DIRS = {"tools", "scripts", "legacy-reference"}
# A path component that excludes a file at ANY depth: a test module never counts as a duplicate
# production copy, and these prune non-source noise (build caches, venvs, vcs metadata) that would
# otherwise be scanned when walking from the real repository root.
_EXCLUDED_ANYWHERE_DIRS = {
    "tests",
    "__pycache__",
    "node_modules",
    "build",
    "dist",
}


def _prune(dirnames: list[str], *, is_top_level: bool) -> None:
    dirnames[:] = [
        d
        for d in dirnames
        if d not in _EXCLUDED_ANYWHERE_DIRS
        and not d.startswith(".")
        and not (is_top_level and d in _EXCLUDED_TOP_LEVEL_DIRS)
    ]


@dataclass(frozen=True)
class Violation:
    code: str
    location: str
    message: str

    def __str__(self) -> str:
        return f"{self.location}: {self.code} {self.message}"


def _is_non_alnum_class_pattern(pattern: str) -> bool:
    """True for a regex character-class pattern that plausibly targets "everything that isn't a
    letter or digit" — the shape every hand-written slug copy uses (``[^a-z0-9]+`` and its
    case-varied siblings). Deliberately loose (a substring check, not a full regex parse): this
    guardrail's zero-false-positive guarantee comes from requiring ``.lower()`` in the SAME
    function, not from precision here.
    """
    lowered = pattern.lower()
    return lowered.startswith("[^") and "a-z" in lowered and "0-9" in lowered


def _module_level_compiled_patterns(tree: ast.Module) -> dict[str, str]:
    """``NAME = re.compile("...")`` bindings at module scope: name -> pattern text."""
    patterns: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "compile"):
            continue
        if not call.args or not isinstance(call.args[0], ast.Constant):
            continue
        pattern = call.args[0].value
        if not isinstance(pattern, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                patterns[target.id] = pattern
    return patterns


def _is_lower_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "lower"
    )


def _sub_pattern(node: ast.AST, compiled: dict[str, str]) -> str | None:
    """If ``node`` is a ``.sub(...)`` call whose pattern resolves to a known non-alphanumeric
    class — either inline (``re.sub(r"...", ...)``) or via a module-level compiled name
    (``_NAME.sub(...)``) — return that pattern text; else ``None``."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    if node.func.attr != "sub":
        return None
    receiver = node.func.value
    if isinstance(receiver, ast.Name):
        if receiver.id == "re":
            if node.args and isinstance(node.args[0], ast.Constant):
                pattern = node.args[0].value
                if isinstance(pattern, str):
                    return pattern
            return None
        return compiled.get(receiver.id)
    return None


def _matches_duplicate_shape(
    func: ast.FunctionDef | ast.AsyncFunctionDef, compiled: dict[str, str]
) -> bool:
    """True if THIS function's OWN body both calls ``.lower()`` and performs a regex substitution
    whose pattern is a known non-alphanumeric class.

    "Own body" excludes a nested function's, and that exclusion is the whole point of the rule.
    ``ast.walk`` descends into nested definitions, so a function with an unrelated ``.lower()`` in a
    helper and an unrelated substitution beside it would be flagged for a duplication neither half
    commits — and attributed to the wrong function name, which is the worst kind of guardrail
    failure: it sends the reader to a place where nothing is wrong. A nested function is visited on
    its own turn by ``_duplicate_functions`` and judged on its own body, so nothing is missed by
    stopping at the boundary.
    """
    has_lower = False
    has_sub = False
    stack: list[ast.AST] = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue  # judged on its own turn, on its own body
        if _is_lower_call(node):
            has_lower = True
        pattern = _sub_pattern(node, compiled)
        if pattern is not None and _is_non_alnum_class_pattern(pattern):
            has_sub = True
        if has_lower and has_sub:
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def _duplicate_functions(py_path: Path) -> set[str]:
    """The names of every top-level-or-nested function in ``py_path`` matching the duplicate
    shape."""
    try:
        source = py_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, UnicodeDecodeError, SyntaxError):
        return set()
    compiled = _module_level_compiled_patterns(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _matches_duplicate_shape(
            node, compiled
        ):
            found.add(node.name)
    return found


def _scan(repo_root: Path) -> dict[str, set[str]]:
    """Every scanned file (relative posix path) -> the set of duplicate-shaped function names it
    defines.

    Walks the whole repo root, pruning as it goes rather than globbing ``packages/*/src`` /
    ``services/*/src`` directly: the real source tree has nothing else under it once ``tests/``,
    ``tools/``, ``scripts/``, ``legacy-reference/`` and build/VCS noise are pruned, and pruning
    (rather than requiring the ``src/`` layout by name) is what keeps this correct for a caller
    that hands in a differently-shaped tree.
    """
    results: dict[str, set[str]] = {}
    for dirpath, dirnames, filenames in os.walk(repo_root):
        current = Path(dirpath)
        is_top_level = current == repo_root
        _prune(dirnames, is_top_level=is_top_level)
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            py = current / filename
            found = _duplicate_functions(py)
            if found:
                rel = py.relative_to(repo_root).as_posix()
                results[rel] = found
    return results


def _load_manifest(manifest_path: Path) -> tuple[list[dict], list[Violation]]:
    """Parse the manifest's ``allow`` list; malformed entries become SLUG002, the entry itself is
    dropped from further processing (never silently passed or silently allow-listing a whole
    file)."""
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return [], [Violation("SLUG002", str(manifest_path), f"manifest unreadable: {exc}")]
    if not isinstance(data, dict):
        return [], [Violation("SLUG002", str(manifest_path), "manifest root is not a mapping")]
    raw_allow = data.get("allow") or []
    if not isinstance(raw_allow, list):
        return [], [Violation("SLUG002", str(manifest_path), "`allow` is not a list")]

    entries: list[dict] = []
    violations: list[Violation] = []
    for i, entry in enumerate(raw_allow):
        loc = f"{manifest_path}#allow[{i}]"
        if not isinstance(entry, dict):
            violations.append(Violation("SLUG002", loc, "allow-list entry is not a mapping"))
            continue
        path = entry.get("path")
        function = entry.get("function")
        if not isinstance(path, str) or not path:
            violations.append(Violation("SLUG002", loc, "allow-list entry has no `path`"))
            continue
        if not isinstance(function, str) or not function:
            violations.append(Violation("SLUG002", loc, "allow-list entry has no `function`"))
            continue
        if not entry.get("reason"):
            violations.append(Violation("SLUG002", loc, "allow-list entry has no `reason`"))
            continue
        entries.append({"path": path, "function": function, "reason": entry["reason"]})
    return entries, violations


def check(manifest_path: Path, repo_root: Path) -> list[Violation]:
    entries, violations = _load_manifest(manifest_path)
    found = _scan(repo_root)

    allowed: set[tuple[str, str]] = {(e["path"], e["function"]) for e in entries}

    for path, functions in found.items():
        for function in sorted(functions):
            if (path, function) not in allowed:
                violations.append(
                    Violation(
                        "SLUG001",
                        f"{path}:{function}",
                        "hand-written copy of the shared plain-text slug normaliser "
                        "(.lower() + a non-alphanumeric-class regex substitution) is not "
                        "allow-listed — repoint it at oraclous_ohm._slug.basic_slug, or add a "
                        "reviewed allow-list entry with a reason if it must stay separate",
                    )
                )

    for entry in entries:
        path, function = entry["path"], entry["function"]
        if function not in found.get(path, set()):
            violations.append(
                Violation(
                    "SLUG003",
                    f"{path}:{function}",
                    "stale allow-list entry — this file/function no longer exists or no longer "
                    "matches the hand-written-copy shape; remove the entry",
                )
            )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--repo-root", type=Path, default=_REPO_ROOT)
    args = parser.parse_args(argv)

    violations = check(args.manifest, args.repo_root)
    if violations:
        print("Slug-duplication guardrail FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print("Slug-duplication guardrail passed (no un-reviewed hand-written slug copies).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
