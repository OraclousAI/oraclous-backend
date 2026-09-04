"""Tests for the slug-duplication guardrail (#731) — SLUG001-003.

#731's design collapses five hand-written twins of ``oraclous_ohm._slug.basic_slug`` (and one more
undocumented copy) onto the shared implementation, and adds this guardrail so a NEW hand-written
copy fails CI instead of drifting again. The detection rule is deliberately narrow: flag a function
only when it BOTH calls ``.lower()`` AND performs a regex substitution whose pattern is a known
non-alphanumeric class (resolving a name bound to a module-level ``re.compile(...)``, not just an
inline ``re.sub`` call). The ``and`` is what gives the guardrail zero false positives — a function
that only lower-cases (``policy._registry_of``'s shape, kept deliberately different — see
``services/harness-runtime-service/tests/unit/test_policy.py``) or only calls an ALREADY-shared slug
function (``registry_client._ref_slug``'s shape) must never trip it.

RED until the ``[impl]`` adds ``tools/lint/check_slug_duplication.py`` +
``tools/lint/slug_duplication.yaml``. Per ``.claude/rules/tests-seam-imports.md``, the module import
is function-local so pytest collection stays clean for the whole repo in the meantime — never
converted into a skip.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def _check(manifest: Path, repo_root: Path) -> list:
    """The seam under test. Function-local: RED until the [impl] lands, never a skip."""
    from tools.lint.check_slug_duplication import check

    return check(manifest, repo_root)


def _codes(manifest: Path, repo_root: Path) -> list[str]:
    return [v.code for v in _check(manifest, repo_root)]


def _manifest(tmp_path: Path, allow: list[dict] | None = None) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"schema_version": 1, "allow": allow or []}), encoding="utf-8")
    return path


def _write(repo_root: Path, rel_path: str, source: str) -> Path:
    target = repo_root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(source), encoding="utf-8")
    return target


# ── shapes that MUST be flagged (SLUG001) ─────────────────────────────────────────────────────────

_INLINE_COPY = """
    import re

    def slugify(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
"""

_COMPILED_COPY = """
    import re

    _NON_ALNUM = re.compile(r"[^a-z0-9]+")

    def _slug(text: str) -> str:
        return _NON_ALNUM.sub("-", text.lower()).strip("-")
"""


def test_a_hand_written_inline_slug_normaliser_is_flagged(tmp_path: Path) -> None:
    """The ``mapping.slugify`` / auth ``organisations.slugify`` shape: an inline ``re.sub`` call
    beside a ``.lower()``."""
    _write(tmp_path, "pkg/mod.py", _INLINE_COPY)
    manifest = _manifest(tmp_path)
    assert "SLUG001" in _codes(manifest, tmp_path)


def test_a_module_level_compiled_pattern_plus_sub_is_flagged_too(tmp_path: Path) -> None:
    """The AST walker must resolve a name bound to ``re.compile(...)`` at module level, not just an
    inline ``re.sub`` call — this is the ``registry_client._slug`` / ``resolution_slug`` shape."""
    _write(tmp_path, "pkg/mod.py", _COMPILED_COPY)
    manifest = _manifest(tmp_path)
    assert "SLUG001" in _codes(manifest, tmp_path)


# ── shapes that must NEVER be flagged (the zero-false-positive guarantee) ─────────────────────────


def test_lower_without_a_substitution_is_not_flagged(tmp_path: Path) -> None:
    """The ``_registry_of`` shape (harness-runtime ``domain/policy.py``): ``.lower()`` alone is a
    case-fold, not a slug normaliser. Flagging it would push a repoint that fails OPEN a governance
    allow-list glob (``org:*``) — see ``test_policy.py``."""
    _write(
        tmp_path,
        "pkg/mod.py",
        """
        def _registry_of(ref: str) -> str:
            return ref.split("/", 1)[0].strip().lower()
        """,
    )
    manifest = _manifest(tmp_path)
    assert "SLUG001" not in _codes(manifest, tmp_path)


def test_a_function_calling_the_shared_primitive_is_not_flagged(tmp_path: Path) -> None:
    """The ``_ref_slug`` shape: it calls the ALREADY-shared ``basic_slug`` on an already-split tail,
    so it is a CALLER, not a new copy, even though it has neither ``.lower()`` nor a regex import of
    its own."""
    _write(
        tmp_path,
        "pkg/mod.py",
        """
        from oraclous_ohm._slug import basic_slug

        def _ref_slug(ref: str) -> str:
            tail = ref.split("/")[-1].split("@")[0]
            return basic_slug(tail)
        """,
    )
    manifest = _manifest(tmp_path)
    assert "SLUG001" not in _codes(manifest, tmp_path)


def test_an_allow_listed_function_is_not_flagged(tmp_path: Path) -> None:
    """The canonical definition site itself (``oraclous_ohm._slug.basic_slug``) necessarily matches
    the SLUG001 shape — it IS the hand-written normaliser everything else now calls. An allow-list
    entry is how the guardrail tells that apart from a new, un-reviewed copy."""
    _write(tmp_path, "pkg/mod.py", _INLINE_COPY)
    manifest = _manifest(
        tmp_path,
        allow=[{"path": "pkg/mod.py", "function": "slugify", "reason": "the definition site"}],
    )
    assert "SLUG001" not in _codes(manifest, tmp_path)


def test_test_files_are_excluded_from_the_scan(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_something.py", _INLINE_COPY)
    manifest = _manifest(tmp_path)
    assert _codes(manifest, tmp_path) == []


def test_service_test_directories_are_excluded_from_the_scan(tmp_path: Path) -> None:
    _write(tmp_path, "services/some-service/tests/unit/test_something.py", _INLINE_COPY)
    manifest = _manifest(tmp_path)
    assert _codes(manifest, tmp_path) == []


def test_tools_and_scripts_directories_are_excluded_from_the_scan(tmp_path: Path) -> None:
    _write(tmp_path, "tools/lint/some_check.py", _INLINE_COPY)
    _write(tmp_path, "scripts/one_off.py", _INLINE_COPY)
    manifest = _manifest(tmp_path)
    assert _codes(manifest, tmp_path) == []


def test_the_legacy_reference_tree_is_excluded_from_the_scan(tmp_path: Path) -> None:
    _write(tmp_path, "legacy-reference/old-backend/mod.py", _INLINE_COPY)
    manifest = _manifest(tmp_path)
    assert _codes(manifest, tmp_path) == []


# ── manifest hygiene ────────────────────────────────────────────────────────────────────────────


def test_a_malformed_allow_list_entry_is_slug002_not_a_silent_pass(tmp_path: Path) -> None:
    """An allow-list entry missing ``function`` must fail loud, not silently pass everything (which
    would let a typo'd allow-list entry protect nothing) and not silently allow-list the whole
    file (which would swallow the real duplicate it should have named)."""
    _write(tmp_path, "pkg/mod.py", _INLINE_COPY)
    manifest = _manifest(tmp_path, allow=[{"path": "pkg/mod.py", "reason": "missing function key"}])
    assert "SLUG002" in _codes(manifest, tmp_path)


def test_a_stale_allow_list_entry_naming_a_missing_function_is_slug003(tmp_path: Path) -> None:
    """An allow-list entry naming a function that does not exist in the file (typo'd, renamed,
    deleted) is dead weight a reviewer can no longer verify against real code — fail, don't pass."""
    _write(tmp_path, "pkg/mod.py", _INLINE_COPY)
    manifest = _manifest(
        tmp_path,
        allow=[{"path": "pkg/mod.py", "function": "no_such_function", "reason": "ghost entry"}],
    )
    assert "SLUG003" in _codes(manifest, tmp_path)


def test_a_stale_allow_list_entry_naming_a_missing_file_is_slug003(tmp_path: Path) -> None:
    manifest = _manifest(
        tmp_path,
        allow=[{"path": "pkg/does_not_exist.py", "function": "slugify", "reason": "ghost file"}],
    )
    assert "SLUG003" in _codes(manifest, tmp_path)


def test_a_stale_allow_list_entry_naming_a_function_that_is_not_actually_a_duplicator_is_slug003(
    tmp_path: Path,
) -> None:
    """An allow-list entry naming a real function that does NOT match the SLUG001 shape (e.g. the
    ``_registry_of`` shape) documents nothing — it can only ever be stale, so it fails the same way
    a missing function does."""
    _write(
        tmp_path,
        "pkg/mod.py",
        """
        def _registry_of(ref: str) -> str:
            return ref.split("/", 1)[0].strip().lower()
        """,
    )
    manifest = _manifest(
        tmp_path,
        allow=[{"path": "pkg/mod.py", "function": "_registry_of", "reason": "not a duplicator"}],
    )
    assert "SLUG003" in _codes(manifest, tmp_path)


# ── the real repository ─────────────────────────────────────────────────────────────────────────


def test_the_real_repository_is_clean() -> None:
    """Stays RED until the [impl] repoints every hand-written copy at the shared primitive (allow-
    listing only the one canonical definition site) — this is the actual #731 acceptance bar, not a
    synthetic fixture. Today FIVE plain copies (plus the undocumented ``resolution_slug`` sixth) are
    still hand-written, so once the guardrail module exists this must still fail until the [impl]
    lands both the collapse and the guardrail together."""
    repo_root = Path(__file__).resolve().parents[2]
    manifest = repo_root / "tools" / "lint" / "slug_duplication.yaml"
    assert _check(manifest, repo_root) == []
