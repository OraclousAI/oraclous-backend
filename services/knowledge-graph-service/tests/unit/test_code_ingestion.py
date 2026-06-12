"""Unit tests for S4 code ingestion: the tree-sitter parser, edge resolution, zip walk, and the
source-type routing. Pure + key-free (no Neo4j) — the graph write is covered by the docker smoke.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.services.code.bootstrap import (
    CodeCloneDisabledError,
    bootstrap,
    iter_zip_sources,
    parse_manifest,
)
from oraclous_knowledge_graph_service.services.code.embeddings import (
    generate_embeddings,
    make_optional_embedder,
)
from oraclous_knowledge_graph_service.services.code.parser import (
    language_for,
    parse_source,
    resolve_edges,
)
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
    names = {path for path, _ in iter_zip_sources(buf.getvalue())}
    assert names == {"pkg/a.py", "pkg/b.ts"}


def test_source_type_routing_for_code() -> None:
    assert source_type_for("repo.zip") == "code"
    assert source_type_for("module.py") == "code"
    assert source_type_for("app.tsx") == "code"


# ── Stage 0 — repository bootstrap: dependency-manifest parsing ───────────────────────────────


def test_parse_requirements_txt() -> None:
    raw = b"# comment\nrequests>=2.0\nflask==3.1.0\n\n-e .\nnumpy\n"
    deps = parse_manifest("requirements.txt", raw)
    by_name = {d.name: d.version_constraint for d in deps}
    assert by_name["requests"] == ">=2.0"
    assert by_name["flask"] == "==3.1.0"
    assert by_name["numpy"] == ""


def test_parse_package_json_sections_and_dep_type() -> None:
    raw = b'{"dependencies": {"react": "^18.0.0"}, "devDependencies": {"jest": "^29.0.0"}}'
    deps = {d.name: (d.version_constraint, d.dep_type) for d in parse_manifest("package.json", raw)}
    assert deps["react"] == ("^18.0.0", "runtime")
    assert deps["jest"] == ("^29.0.0", "dev")


def test_parse_go_mod() -> None:
    raw = b"module example\n\nrequire (\n\tgithub.com/pkg/errors v0.9.1\n)\n"
    deps = parse_manifest("go.mod", raw)
    assert any(d.name == "github.com/pkg/errors" and d.version_constraint == "v0.9.1" for d in deps)


def test_parse_pom_xml_namespaced() -> None:
    raw = b"""<project xmlns="http://maven.apache.org/POM/4.0.0">
      <dependencies>
        <dependency><groupId>org.junit</groupId><artifactId>junit</artifactId>
        <version>5.9.0</version><scope>test</scope></dependency>
      </dependencies>
    </project>"""
    deps = parse_manifest("pom.xml", raw)
    assert any(d.name == "org.junit:junit" and d.version_constraint == "5.9.0" for d in deps)


def test_bootstrap_zip_collects_sources_and_dependencies() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pkg/a.py", "def a(): pass")
        zf.writestr("requirements.txt", "requests==2.31.0\n")
        zf.writestr("node_modules/d/package.json", '{"dependencies": {"x": "1"}}')  # vendored
    sources, deps = bootstrap(document="repo.zip", data=buf.getvalue())
    assert {p for p, _ in sources} == {"pkg/a.py"}
    assert [d.name for d in deps] == ["requests"]  # the vendored manifest is skipped


def test_bootstrap_single_file_has_no_manifests() -> None:
    sources, deps = bootstrap(document="m.py", data=b"def f(): pass")
    assert [p for p, _ in sources] == ["m.py"]
    assert deps == []


def test_git_url_rejected_when_clone_disabled() -> None:
    with pytest.raises(CodeCloneDisabledError):
        bootstrap(document="x", data=b"", git_url="https://example.com/r.git", clone_enabled=False)


# ── Stage 4 — embeddings (fail-soft) ──────────────────────────────────────────────────────────


class _FakeEmbedder:
    dim = 512

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t))] * self.dim for t in texts]


_NODE_SYMS = [
    {
        "label": "Function",
        "qualified_name": "m.f",
        "properties": {"signature": "(x)", "docstring": "d"},
    },
    {"label": "Class", "qualified_name": "m.C", "properties": {"docstring": "klass"}},
    {"label": "Variable", "qualified_name": "m.V", "properties": {}},  # not embeddable
]


def test_generate_embeddings_only_functions_and_classes() -> None:
    rows = generate_embeddings(_NODE_SYMS, _FakeEmbedder())
    assert {r["qualified_name"] for r in rows} == {"m.f", "m.C"}  # Variable excluded
    assert all(len(r["embedding"]) == 512 for r in rows)


def test_generate_embeddings_fail_soft_without_embedder() -> None:
    # No embedder (the no-key skip path) -> empty, never a crash.
    assert generate_embeddings(_NODE_SYMS, None) == []


def test_make_optional_embedder_skips_when_openai_without_key() -> None:
    settings = Settings(embedder="openai", openai_api_key=None)
    assert make_optional_embedder(settings) is None  # fail-soft, no crash


def test_make_optional_embedder_hashing_is_keyfree() -> None:
    embedder = make_optional_embedder(Settings(embedder="hashing"))
    assert embedder is not None
    assert embedder.dim == 512
