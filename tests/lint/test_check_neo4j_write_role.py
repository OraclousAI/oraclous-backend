"""Tests for the Neo4j write-role bypass guardrail (ORAA-53 / T6).

Verifies that check_neo4j_write_role fires on each bypass pattern and passes
clean KGS-style code.
"""

from __future__ import annotations

import pytest
from tools.lint.check_neo4j_write_role import check_source

pytestmark = pytest.mark.unit


def _rules(src: str) -> set[str]:
    return {v.rule for v in check_source(src)}


# ---------------------------------------------------------------------------
# NEO4J001 — admin env vars inside KGS
# ---------------------------------------------------------------------------


def test_neo4j001_neo4j_uri_environ_get() -> None:
    src = 'import os\nuri = os.environ.get("NEO4J_URI")\n'
    assert "NEO4J001" in _rules(src)


def test_neo4j001_neo4j_user_environ_subscript() -> None:
    src = 'import os\nuser = os.environ["NEO4J_USER"]\n'
    assert "NEO4J001" in _rules(src)


def test_neo4j001_neo4j_password_getenv() -> None:
    src = 'import os\npwd = os.getenv("NEO4J_PASSWORD")\n'
    assert "NEO4J001" in _rules(src)


def test_neo4j001_neo4j_auth_flagged() -> None:
    src = 'import os\nauth = os.environ.get("NEO4J_AUTH")\n'
    assert "NEO4J001" in _rules(src)


def test_neo4j001_kgs_vars_not_flagged() -> None:
    src = 'import os\nuri = os.environ.get("KGS_NEO4J_URI")\n'
    assert "NEO4J001" not in _rules(src)


def test_neo4j001_krs_vars_not_flagged() -> None:
    # KRS vars are irrelevant to the KGS write-role check (separate guardrail)
    src = 'import os\nuri = os.environ.get("KRS_NEO4J_URI")\n'
    assert "NEO4J001" not in _rules(src)


# ---------------------------------------------------------------------------
# NEO4J002 — hardcoded bolt:// / neo4j:// URIs
# ---------------------------------------------------------------------------


def test_neo4j002_bolt_uri_literal() -> None:
    src = 'uri = "bolt://neo4j:7687"\n'
    assert "NEO4J002" in _rules(src)


def test_neo4j002_neo4j_scheme_uri_literal() -> None:
    src = 'uri = "neo4j://localhost:7687"\n'
    assert "NEO4J002" in _rules(src)


def test_neo4j002_bolt_plus_s_uri_literal() -> None:
    src = 'uri = "bolt+s://neo4j:7687"\n'
    assert "NEO4J002" in _rules(src)


def test_neo4j002_env_var_read_not_flagged() -> None:
    src = 'import os\nuri = os.environ.get("KGS_NEO4J_URI")\n'
    assert "NEO4J002" not in _rules(src)


def test_neo4j002_plain_string_not_flagged() -> None:
    src = 'msg = "connecting to graph database"\n'
    assert "NEO4J002" not in _rules(src)


# ---------------------------------------------------------------------------
# Clean KGS-style code passes all checks
# ---------------------------------------------------------------------------


def test_clean_kgs_module_passes() -> None:
    src = """\
from __future__ import annotations
import os
from neo4j import GraphDatabase

def make_driver():
    uri = os.environ.get("KGS_NEO4J_URI", "")
    user = os.environ.get("KGS_NEO4J_USER", "kgs_writer")
    password = os.environ.get("KGS_NEO4J_PASSWORD", "")
    return GraphDatabase.driver(uri, auth=(user, password))
"""
    assert _rules(src) == set()
