"""Tests for the Neo4j read-role bypass guardrail (ORAA-58 / T6).

Verifies that check_neo4j_read_role fires on each bypass pattern and passes
clean KRS-style code.
"""

from __future__ import annotations

import pytest
from tools.lint.check_neo4j_read_role import check_source

pytestmark = pytest.mark.unit


def _rules(src: str) -> set[str]:
    return {v.rule for v in check_source(src)}


# ---------------------------------------------------------------------------
# NEO4J_KRS001 — Cypher write keywords in string literals
# ---------------------------------------------------------------------------


def test_krs001_create_in_string_literal() -> None:
    src = 'query = "CREATE (n:Node {name: $name}) RETURN n"\n'
    assert "NEO4J_KRS001" in _rules(src)


def test_krs001_merge_in_string_literal() -> None:
    src = 'query = "MATCH (n) MERGE (n)-[:REL]->(m) RETURN n"\n'
    assert "NEO4J_KRS001" in _rules(src)


def test_krs001_delete_in_string_literal() -> None:
    src = 'query = "MATCH (n) DELETE n"\n'
    assert "NEO4J_KRS001" in _rules(src)


def test_krs001_remove_in_string_literal() -> None:
    src = 'query = "MATCH (n) REMOVE n.property RETURN n"\n'
    assert "NEO4J_KRS001" in _rules(src)


def test_krs001_set_property_in_string_literal() -> None:
    src = 'query = "MATCH (n) SET n.name = $name RETURN n"\n'
    assert "NEO4J_KRS001" in _rules(src)


def test_krs001_clean_match_return_not_flagged() -> None:
    src = 'query = "MATCH (n:Node) WHERE n.graph_id = $graph_id RETURN n"\n'
    assert "NEO4J_KRS001" not in _rules(src)


def test_krs001_python_set_builtin_not_flagged() -> None:
    src = "items = set(result)\n"
    assert "NEO4J_KRS001" not in _rules(src)


def test_krs001_lowercase_create_in_prose_not_flagged() -> None:
    # lowercase 'create' in English prose (docstring, comment) is not flagged —
    # Cypher write keywords are conventional uppercase; case-sensitive matching
    # avoids false positives on method names and docstrings.
    src = 'msg = "cannot create node"\n'
    assert "NEO4J_KRS001" not in _rules(src)


# ---------------------------------------------------------------------------
# NEO4J_KRS002 — admin env vars
# ---------------------------------------------------------------------------


def test_krs002_neo4j_uri_environ_get() -> None:
    src = 'import os\nuri = os.environ.get("NEO4J_URI")\n'
    assert "NEO4J_KRS002" in _rules(src)


def test_krs002_neo4j_user_environ_subscript() -> None:
    src = 'import os\nuser = os.environ["NEO4J_USER"]\n'
    assert "NEO4J_KRS002" in _rules(src)


def test_krs002_neo4j_password_getenv() -> None:
    src = 'import os\npwd = os.getenv("NEO4J_PASSWORD")\n'
    assert "NEO4J_KRS002" in _rules(src)


def test_krs002_neo4j_auth_flagged() -> None:
    src = 'import os\nauth = os.environ.get("NEO4J_AUTH")\n'
    assert "NEO4J_KRS002" in _rules(src)


def test_krs002_krs_vars_not_flagged() -> None:
    src = 'import os\nuri = os.environ.get("KRS_NEO4J_URI")\n'
    assert "NEO4J_KRS002" not in _rules(src)


# ---------------------------------------------------------------------------
# NEO4J_KRS003 — KGS write-role env vars
# ---------------------------------------------------------------------------


def test_krs003_kgs_uri_flagged() -> None:
    src = 'import os\nuri = os.environ.get("KGS_NEO4J_URI")\n'
    assert "NEO4J_KRS003" in _rules(src)


def test_krs003_kgs_user_flagged() -> None:
    src = 'import os\nuser = os.environ["KGS_NEO4J_USER"]\n'
    assert "NEO4J_KRS003" in _rules(src)


def test_krs003_kgs_password_flagged() -> None:
    src = 'import os\npwd = os.getenv("KGS_NEO4J_PASSWORD")\n'
    assert "NEO4J_KRS003" in _rules(src)


def test_krs003_krs_vars_not_flagged() -> None:
    src = 'import os\npwd = os.environ.get("KRS_NEO4J_PASSWORD")\n'
    assert "NEO4J_KRS003" not in _rules(src)


# ---------------------------------------------------------------------------
# NEO4J_KRS004 — hardcoded bolt URIs
# ---------------------------------------------------------------------------


def test_krs004_bolt_uri_literal() -> None:
    src = 'uri = "bolt://neo4j:7687"\n'
    assert "NEO4J_KRS004" in _rules(src)


def test_krs004_neo4j_scheme_uri_literal() -> None:
    src = 'uri = "neo4j://localhost:7687"\n'
    assert "NEO4J_KRS004" in _rules(src)


def test_krs004_bolt_plus_s_uri_literal() -> None:
    src = 'uri = "bolt+s://neo4j:7687"\n'
    assert "NEO4J_KRS004" in _rules(src)


def test_krs004_env_var_read_not_flagged() -> None:
    src = 'import os\nuri = os.environ.get("KRS_NEO4J_URI")\n'
    assert "NEO4J_KRS004" not in _rules(src)


# ---------------------------------------------------------------------------
# Clean KRS-style code passes all checks
# ---------------------------------------------------------------------------


def test_clean_krs_module_passes() -> None:
    src = """\
from __future__ import annotations
import os
from neo4j_graphrag.retrievers import VectorRetriever

def make_retriever(driver, index_name: str) -> VectorRetriever:
    uri = os.environ.get("KRS_NEO4J_URI", "")
    user = os.environ.get("KRS_NEO4J_USER", "krs_reader")
    password = os.environ.get("KRS_NEO4J_PASSWORD", "")
    query = "MATCH (n:Chunk) WHERE n.graph_id = $graph_id RETURN n.text AS text"
    return VectorRetriever(driver=driver, index_name=index_name)
"""
    assert _rules(src) == set()
