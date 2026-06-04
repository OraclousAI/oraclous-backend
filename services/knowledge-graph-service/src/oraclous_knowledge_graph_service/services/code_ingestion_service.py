"""Code ingestion use-case (ORAA-4 §21 services layer).

Walk a source tree (a .zip of sources, or one source file), tree-sitter-parse each supported file
into a deterministic code graph, resolve cross-file edges, and write via the org-scoped code writer.
Key-free and synchronous (the parser + sync Neo4j driver), so the worker calls it via
`asyncio.to_thread`. Caps (max files / max file bytes) guard against pathological repos.
"""

from __future__ import annotations

import io
import zipfile

from oraclous_knowledge_graph_service.repositories.code_write_repository import (
    CodeGraphWriteRepository,
)
from oraclous_knowledge_graph_service.services.code.parser import (
    language_for,
    parse_source,
    resolve_edges,
)

_MAX_FILES = 5000
_MAX_FILE_BYTES = 2_000_000
_SKIP_DIR_PARTS = {".git", "node_modules", "__pycache__", "venv", ".venv", "dist", "build"}
_NODE_SYMBOL_TYPES = {"Function", "Class", "Variable"}


def is_code(source_type: str) -> bool:
    return (source_type or "").lower() == "code"


class CodeIngestionError(Exception):
    """Code ingestion failed (not a zip / no parseable source)."""


def _iter_zip(zip_bytes: bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir() or info.file_size > _MAX_FILE_BYTES:
                continue
            parts = info.filename.split("/")
            if any(p in _SKIP_DIR_PARTS or p.startswith(".") for p in parts[:-1]):
                continue
            if language_for(info.filename) is None:
                continue
            yield info.filename, zf.read(info)


class CodeIngestionService:
    def __init__(self, *, driver, organisation_id: str, database: str | None = None) -> None:
        self._driver = driver
        self._org = organisation_id
        self._db = database

    def ingest(self, *, graph_id: str, document: str, data: bytes) -> dict:
        if document.lower().endswith(".zip") or data[:2] == b"PK":
            sources = list(_iter_zip(data))
        elif language_for(document):
            sources = [(document, data)]
        else:
            raise CodeIngestionError(f"unsupported code source: {document!r}")
        if not sources:
            raise CodeIngestionError("no parseable source files found")
        if len(sources) > _MAX_FILES:
            sources = sources[:_MAX_FILES]

        files: list[dict] = []
        symbols = []
        warnings = 0
        for path, raw in sources:
            parsed = parse_source(path, raw)
            if parsed is None:
                continue
            meta, syms = parsed
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

        writer = CodeGraphWriteRepository(
            self._driver, graph_id=graph_id, organisation_id=self._org, database=self._db
        )
        writer.replace_files(files)
        writer.write_symbols(node_symbols)
        writer.write_edges(calls=calls, inherits=inherits, imports=imports)

        counts = {
            "files": len(files),
            "functions": sum(1 for s in node_symbols if s["label"] == "Function"),
            "classes": sum(1 for s in node_symbols if s["label"] == "Class"),
            "variables": sum(1 for s in node_symbols if s["label"] == "Variable"),
            "calls": len(calls),
            "imports": len(imports),
            "inherits": len(inherits),
            "files_without_symbols": warnings,
        }
        return counts
