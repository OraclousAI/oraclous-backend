"""Tests proving the TDD-window test-import hygiene guardrail fires (0b).

The importability resolver is injected so these are deterministic and do not
depend on which seams happen to be built on the tree under test.
"""

from __future__ import annotations

import pytest
from tools.lint.check_test_imports import check_source

pytestmark = pytest.mark.unit


def _resolver(built: dict[str, set[str]]):
    """A fake resolver: ``built`` maps an importable module to the names it exposes."""

    def resolve(module: str, names: tuple[str, ...]) -> bool:
        if module not in built:
            return False
        return all(name in built[module] for name in names)

    return resolve


def _rules(src: str, built: dict[str, set[str]] | None = None) -> list:
    return check_source(src, "tests/unit/test_x.py", resolver=_resolver(built or {}))


# --- TST001: module-level import of a not-yet-built intra-repo seam -----------


def test_tst001_module_level_unbuilt_from_import_flagged() -> None:
    src = "from oraclous_rebac import ReBACEngine\n\n\ndef test_x():\n    assert ReBACEngine\n"
    assert {v.rule for v in _rules(src)} == {"TST001"}


def test_tst001_module_level_unbuilt_plain_import_flagged() -> None:
    src = "import oraclous_substrate.access\n"
    assert {v.rule for v in _rules(src)} == {"TST001"}


def test_tst001_function_local_import_not_flagged() -> None:
    # The convention: import the not-yet-built seam inside the test body.
    src = "def test_x():\n    from oraclous_rebac import ReBACEngine\n    assert ReBACEngine\n"
    assert _rules(src) == []


def test_tst001_built_seam_module_level_not_flagged() -> None:
    src = "from oraclous_rebac import ReBACEngine\n"
    assert _rules(src, built={"oraclous_rebac": {"ReBACEngine"}}) == []


def test_tst001_built_module_but_missing_symbol_flagged() -> None:
    # The exact failure: module imports, symbol does not exist yet.
    src = "from oraclous_rebac import ReBACEngine\n"
    rules = _rules(src, built={"oraclous_rebac": {"OtherThing"}})
    assert {v.rule for v in rules} == {"TST001"}


def test_tst001_third_party_import_not_flagged() -> None:
    src = "import pytest\nfrom neo4j import GraphDatabase\n"
    assert _rules(src) == []


def test_tst001_relative_import_not_flagged() -> None:
    # A relative import is within the test package itself, never the cross-seam case.
    src = "from . import conftest_helpers\nfrom .util import make_org\n"
    assert _rules(src) == []


def test_tst001_built_seam_in_fixture_not_flagged() -> None:
    src = (
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def engine():\n"
        "    from oraclous_rebac import ReBACEngine\n"
        "    return ReBACEngine()\n"
    )
    assert _rules(src) == []


# --- TST002: skip-masking of an intra-repo seam (forbidden) -------------------


def test_tst002_importorskip_intra_repo_flagged() -> None:
    src = "import pytest\n\nmod = pytest.importorskip('oraclous_rebac')\n"
    assert {v.rule for v in _rules(src)} == {"TST002"}


def test_tst002_importorskip_third_party_not_flagged() -> None:
    # Skipping on a genuinely-optional third-party dependency is legitimate.
    src = "import pytest\n\nmod = pytest.importorskip('lxml')\n"
    assert _rules(src) == []


def test_tst002_try_except_importerror_skip_flagged() -> None:
    src = (
        "import pytest\n\n"
        "try:\n"
        "    from oraclous_rebac import ReBACEngine\n"
        "except ImportError:\n"
        "    pytest.skip('rebac not built', allow_module_level=True)\n"
    )
    rules = _rules(src)
    # The masking construct is reported as TST002, not double-counted as TST001.
    assert {v.rule for v in rules} == {"TST002"}


def test_tst002_try_except_unrelated_not_flagged() -> None:
    # try/except around an intra-repo import that does NOT skip is fine (e.g. a
    # genuine fallback) — only the skip-masking form is forbidden.
    src = (
        "try:\n"
        "    from oraclous_rebac import ReBACEngine\n"
        "except ImportError:\n"
        "    ReBACEngine = None\n"
    )
    # Still TST001 (module-level unbuilt import), but not TST002.
    assert {v.rule for v in _rules(src)} == {"TST001"}
