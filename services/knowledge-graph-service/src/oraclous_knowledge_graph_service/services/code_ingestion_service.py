"""Code ingestion use-case — the full 6-stage pipeline (ORAA-4 §21 services layer, #305).

Lift-and-reshape of legacy `develop@84152635 code_parser_service` (all six stages), org+graph
scoped (the scope is server-injected at construction; the caller can never override it):

  Stage 0 — Repository bootstrap : resolve the source tree (uploaded zip/file OR a flag-gated
            git clone), walk supported files, parse dependency manifests -> :Dependency nodes.
  Stage 1 — Delta detection      : SHA-256 hash each file, compare to the existing :File nodes,
            split new/changed/unchanged, mark a changed file's existing symbols `stale_at`.
  Stage 2 — AST parse            : tree-sitter parse each changed/new file (services/code/parser).
  Stage 3 — Cross-file resolve   : CALLS / IMPORTS / INHERITS by qualified name (parser.resolve).
  Stage 4 — Embeddings           : embed Function/Class (qname+signature+docstring), fail-soft if
            no key; write the `embedding` property + ensure the vector index.
  Stage 5 — Write                : the org-scoped CodeGraphWriteRepository (idempotent MERGEs).
  Stage 6 — Stale cleanup        : NOT here — an async Celery sweep (tasks/code_stale_tasks).

Key-free and synchronous (the parser + sync Neo4j driver), so the worker calls it via
`asyncio.to_thread`. Caps (max files / max file bytes) guard against pathological repos.
"""

from __future__ import annotations

from oraclous_knowledge_graph_service.core.config import Settings, get_settings
from oraclous_knowledge_graph_service.repositories.code_write_repository import (
    CodeGraphWriteRepository,
)
from oraclous_knowledge_graph_service.services.code.bootstrap import (
    CodeIngestionSourceError,
    bootstrap,
)
from oraclous_knowledge_graph_service.services.code.embeddings import (
    generate_embeddings,
    make_optional_embedder,
)
from oraclous_knowledge_graph_service.services.code.parser import parse_source, resolve_edges

_NODE_SYMBOL_TYPES = {"Function", "Class", "Variable"}


def is_code(source_type: str) -> bool:
    return (source_type or "").lower() == "code"


# Re-export so existing imports (tests, the worker) keep working after the bootstrap split.
CodeIngestionError = CodeIngestionSourceError


class CodeIngestionService:
    def __init__(
        self,
        *,
        driver,
        organisation_id: str,
        database: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._driver = driver
        self._org = organisation_id
        self._db = database
        self._settings = settings or get_settings()

    def ingest(
        self,
        *,
        graph_id: str,
        document: str,
        data: bytes,
        git_url: str | None = None,
        branch: str = "",
    ) -> dict:
        writer = CodeGraphWriteRepository(
            self._driver, graph_id=graph_id, organisation_id=self._org, database=self._db
        )

        # Stage 0 — bootstrap: resolve the source tree + parse dependency manifests.
        sources, deps = bootstrap(
            document=document,
            data=data,
            git_url=git_url,
            branch=branch,
            clone_enabled=self._settings.code_clone_enabled,
        )
        if not sources:
            raise CodeIngestionError("no parseable source files found")

        # Stage 2 (parse) — done up front so Stage 1's delta has each file's content_hash.
        parsed_files: list[tuple] = []  # (ParsedFile, [RawSymbol])
        for path, raw in sources:
            parsed = parse_source(path, raw)
            if parsed is not None:
                parsed_files.append(parsed)

        # Stage 1 — delta: compare each file's hash to the existing :File node.
        all_paths = [meta.path for meta, _ in parsed_files]
        existing = writer.existing_file_hashes(all_paths)
        new_files, changed_files, unchanged_files = [], [], []
        for meta, syms in parsed_files:
            if meta.path not in existing:
                new_files.append((meta, syms))
            elif existing[meta.path] != meta.content_hash:
                changed_files.append((meta, syms))
            else:
                unchanged_files.append((meta, syms))
        if changed_files:
            writer.mark_symbols_stale([meta.path for meta, _ in changed_files])

        # Only new + changed files are (re)written; unchanged files are skipped (delta idempotency).
        to_write = new_files + changed_files
        files: list[dict] = []
        symbols = []
        warnings = 0
        for meta, syms in to_write:
            files.append(
                {
                    "path": meta.path,
                    "language": meta.language,
                    "content_hash": meta.content_hash,
                    "size_bytes": meta.size_bytes,
                }
            )
            if not syms:
                warnings += 1
            symbols.extend(syms)

        # Stage 3 — cross-file resolve (CALLS / IMPORTS / INHERITS by qualified name).
        calls, imports, inherits = resolve_edges(symbols)
        node_symbols = [
            {
                "label": s.symbol_type,
                "qualified_name": s.qualified_name,
                "file_path": s.file_path,
                "properties": {
                    k: v
                    for k, v in {
                        "name": s.name,
                        "language": s.language,
                        "start_line": s.start_line,
                        "end_line": s.end_line,
                        "signature": s.signature,
                        "is_method": s.is_method,
                        "is_async": s.is_async,
                        "is_test": s.is_test,
                        "docstring": s.docstring,
                        "parent_class": s.parent_class,
                        "type_annotation": s.type_annotation,
                    }.items()
                    if v is not None
                },
            }
            for s in symbols
            if s.symbol_type in _NODE_SYMBOL_TYPES
        ]

        # Stage 4 — embeddings (fail-soft): embed Function/Class and write the `embedding` property
        # (org+graph scoped). The accelerating vector INDEX is deferred to the retriever slice — a
        # label-wide Neo4j vector index cannot be org-scoped, so the org-filtered kNN belongs where
        # the read happens (#294); see CodeGraphWriteRepository for the rationale.
        embedder = make_optional_embedder(self._settings)
        embeddings = generate_embeddings(node_symbols, embedder)

        # Stage 5 — write (idempotent, ordered: dependencies, files, symbols, edges, embeddings).
        dep_rows = [
            {"name": d.name, "version_constraint": d.version_constraint, "dep_type": d.dep_type}
            for d in deps
        ]
        writer.write_dependencies(dep_rows)
        writer.replace_files(files)
        writer.write_symbols(node_symbols)
        writer.write_edges(calls=calls, inherits=inherits, imports=imports)
        if embeddings:
            writer.write_embeddings(embeddings)

        return {
            "files": len(files),
            "files_new": len(new_files),
            "files_changed": len(changed_files),
            "files_unchanged": len(unchanged_files),
            "dependencies": len(dep_rows),
            "functions": sum(1 for s in node_symbols if s["label"] == "Function"),
            "classes": sum(1 for s in node_symbols if s["label"] == "Class"),
            "variables": sum(1 for s in node_symbols if s["label"] == "Variable"),
            "calls": len(calls),
            "imports": len(imports),
            "inherits": len(inherits),
            "embeddings": len(embeddings),
            "files_without_symbols": warnings,
        }
