"""Code ingestion use-case — the full 6-stage pipeline (services layer, #305).

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

import logging
import time

from oraclous_knowledge_graph_service.core.config import Settings, get_settings
from oraclous_knowledge_graph_service.core.redis import RedisLock, RedisLockClient
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

logger = logging.getLogger(__name__)

_NODE_SYMBOL_TYPES = {"Function", "Class", "Variable"}
# Poll interval when another ingest of the same graph holds the lock (advisory; the TTL bounds it).
_LOCK_WAIT_SECONDS = 1.0


def code_ingest_lock_key(*, organisation_id: str, graph_id: str) -> str:
    """The per-(org,graph) advisory lock key the code-ingest critical section AND the Stage-6 sweep
    share (#305) — a re-ingest and a sweep on the same graph serialise on it, and the sweep skips a
    graph that holds it (mid-ingest)."""
    return f"kgs:code_ingest:{organisation_id}:{graph_id}"


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
        lock_client: RedisLockClient | None = None,
    ) -> None:
        self._driver = driver
        self._org = organisation_id
        self._db = database
        self._settings = settings or get_settings()
        # Advisory per-(org,graph) Redis lock client (#305). ``None`` -> lock-off (degrades like the
        # community-detect lock #303): re-ingests of the same graph no longer serialise, but the
        # ingest still runs. The driver import lives in core/redis (STR004) — we only hold a client.
        self._lock_client = lock_client

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

        # The delta-read → write window is the critical section: serialise concurrent re-ingests of
        # the SAME (org, graph) under the advisory lock (#305) so the mark-stale → revive race can't
        # strand a symbol, and so a concurrent Stage-6 sweep can't DETACH-DELETE a node this ingest
        # is reviving. Advisory: no lock client (or a Redis fault) degrades to lock-off (logged).
        lock = RedisLock(
            self._lock_client,
            key=code_ingest_lock_key(organisation_id=self._org, graph_id=graph_id),
            ttl_seconds=self._settings.code_ingest_lock_ttl_seconds,
        )
        token = lock.acquire()
        while token is None:
            # Another ingest of this graph holds the lock; the SET-NX-EX is short — wait it out
            # rather than racing (a worker task, so a brief block is fine; the TTL bounds a crash).
            logger.info(
                "code ingest waiting on in-flight ingest of graph=%s (org=%s)", graph_id, self._org
            )
            time.sleep(_LOCK_WAIT_SECONDS)
            token = lock.acquire()
        try:
            return self._ingest_locked(
                writer=writer, graph_id=graph_id, parsed_files=parsed_files, deps=deps
            )
        finally:
            lock.release(token)

    def _ingest_locked(self, *, writer, graph_id: str, parsed_files: list, deps: list) -> dict:
        # Stage 1 — delta: compare each file's hash to the existing :File node.
        all_paths = [meta.path for meta, _ in parsed_files]
        upload_paths = set(all_paths)
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

        # Deleted files: prior :File paths for this (org, graph) MINUS the current-upload paths.
        # A file that existed but is ABSENT now (a deletion, or the old half of a rename) is never
        # re-written, so we stale-mark its symbols + :File node (Stage 6 reaps them at TTL). The
        # per-upload path scan alone can never see these (they're not in the upload).
        deleted_paths = sorted(writer.all_file_paths() - upload_paths)
        if deleted_paths:
            writer.mark_files_deleted(deleted_paths)

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
        embeddings = generate_embeddings(
            node_symbols, embedder, max_symbols=self._settings.code_max_embed_symbols
        )

        # Stage 5 — write (idempotent, ordered: dependencies, files, symbols, edges, embeddings).
        dep_rows = [
            {"name": d.name, "version_constraint": d.version_constraint, "dep_type": d.dep_type}
            for d in deps
        ]
        writer.write_dependencies(dep_rows)
        writer.upsert_files(files)
        writer.write_symbols(node_symbols)
        writer.write_edges(calls=calls, inherits=inherits, imports=imports)
        if embeddings:
            writer.write_embeddings(embeddings)

        return {
            "files": len(files),
            "files_new": len(new_files),
            "files_changed": len(changed_files),
            "files_unchanged": len(unchanged_files),
            "files_deleted": len(deleted_paths),
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
