"""Unit tests for S4 code ingestion: the tree-sitter parser, edge resolution, zip walk, and the
source-type routing. Pure + key-free (no Neo4j) — the graph write is covered by the docker smoke.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from oraclous_knowledge_graph_service.services.code.parser import (
    language_for,
    parse_source,
    resolve_edges,
)
from oraclous_knowledge_graph_service.services.code_ingestion_service import _iter_zip
from oraclous_knowledge_graph_service.services.extractors import source_type_for

pytestmark = pytest.mark.unit

_PY = b"""import os


class Greeter(Base):
    def greet(self, name):
        return helper(name)


def helper(name):
    return name
"""


def test_language_detection() -> None:
    assert language_for("a/b.py") == "python"
    assert language_for("c.ts") == "typescript"
    assert language_for("d.js") == "javascript"
    assert language_for("e.md") is None


def test_parse_python_symbols_and_qnames() -> None:
    parsed = parse_source("pkg/greeter.py", _PY)
    assert parsed is not None
    meta, symbols = parsed
    assert meta.language == "python"
    assert len(meta.content_hash) == 64
    by_type = {(s.symbol_type, s.qualified_name) for s in symbols}
    assert ("Class", "pkg.greeter.Greeter") in by_type
    assert ("Function", "pkg.greeter.Greeter.greet") in by_type  # no double-module
    assert ("Function", "pkg.greeter.helper") in by_type
    greet = next(s for s in symbols if s.qualified_name == "pkg.greeter.Greeter.greet")
    assert greet.is_method is True


def test_resolve_edges_calls_and_inherits_and_imports() -> None:
    _meta, symbols = parse_source("pkg/greeter.py", _PY)
    calls, imports, inherits = resolve_edges(symbols)
    assert {"caller": "pkg.greeter.Greeter.greet", "callee": "pkg.greeter.helper"} in calls
    assert any(i["target"] == "os" and i["is_internal"] is False for i in imports)
    # Base is not defined in this file -> no INHERITS edge resolved (unresolved bases are dropped)
    assert inherits == []


def test_parse_typescript() -> None:
    ts = b"export class Service {\n  run(x: number) { return x; }\n}\n"
    meta, symbols = parse_source("src/service.ts", ts)
    assert meta.language == "typescript"
    assert any(s.symbol_type == "Class" and s.name == "Service" for s in symbols)


def test_unsupported_file_returns_none() -> None:
    assert parse_source("readme.md", b"# hi") is None


def test_garbage_python_yields_file_only() -> None:
    parsed = parse_source("broken.py", b"def (((")
    assert parsed is not None
    _meta, symbols = parsed
    # tree-sitter is error-tolerant; the run never crashes (zero or partial symbols)
    assert isinstance(symbols, list)


def test_iter_zip_skips_non_source_and_vendored() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pkg/a.py", "def a(): pass")
        zf.writestr("pkg/b.ts", "function b() {}")
        zf.writestr("node_modules/x/y.js", "function vendored() {}")  # skipped
        zf.writestr("README.md", "# doc")  # skipped (not source)
    names = {path for path, _ in _iter_zip(buf.getvalue())}
    assert names == {"pkg/a.py", "pkg/b.ts"}


def test_source_type_routing_for_code() -> None:
    assert source_type_for("repo.zip") == "code"
    assert source_type_for("module.py") == "code"
    assert source_type_for("app.tsx") == "code"
