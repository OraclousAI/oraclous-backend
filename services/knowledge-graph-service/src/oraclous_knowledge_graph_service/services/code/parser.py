"""Tree-sitter code parser (services layer — pure, key-free, no DB).

Faithful port of legacy `develop@84152635 .../services/code_parser_service.py`
(Stages 0–3): walk source files, tree-sitter-parse, extract a deterministic code knowledge graph
(:File/:Module/:Class/:Function/:Variable + DEFINED_IN/METHOD_OF/CALLS/IMPORTS/INHERITS), and
resolve cross-file edges by qualified name. Stable identity = the dotted `qualified_name` (not a
uuid); content_hash (sha256 of file bytes) is the delta-idempotency key. Embeddings (legacy Stage 4)
are out of S4 (key-gated). Python + TypeScript/JavaScript extractors are ported; the loader supports
Go/Java grammars too (extractors are a fast-follow) and degrades to zero symbols if a grammar is
absent — surfaced in stats, never a crash.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LANGUAGE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
}

_PARSERS: dict[str, Any] = {}


@dataclass
class RawSymbol:
    symbol_type: str  # Function | Class | Variable | Module
    name: str
    qualified_name: str
    language: str
    file_path: str
    start_line: int
    end_line: int
    docstring: str | None = None
    signature: str | None = None
    is_async: bool = False
    is_method: bool = False
    is_test: bool = False
    type_annotation: str | None = None
    value_preview: str | None = None
    parent_class: str | None = None
    raw_calls: list[str] = field(default_factory=list)
    raw_imports: list[dict[str, Any]] = field(default_factory=list)
    raw_bases: list[str] = field(default_factory=list)


@dataclass
class ParsedFile:
    path: str
    language: str
    size_bytes: int
    content_hash: str
    is_test: bool


def _get_parser(language: str) -> Any | None:
    if language in _PARSERS:
        return _PARSERS[language]
    parser = None
    try:
        from tree_sitter import Language, Parser

        if language == "python":
            import tree_sitter_python

            lang = Language(tree_sitter_python.language())
        elif language in ("typescript", "tsx"):
            import tree_sitter_typescript

            lang = Language(tree_sitter_typescript.language_typescript())
        elif language == "javascript":
            import tree_sitter_javascript

            lang = Language(tree_sitter_javascript.language())
        elif language == "go":
            import tree_sitter_go

            lang = Language(tree_sitter_go.language())
        elif language == "java":
            import tree_sitter_java

            lang = Language(tree_sitter_java.language())
        else:
            _PARSERS[language] = None
            return None
        parser = Parser(lang)
    except Exception as exc:  # noqa: BLE001 — missing grammar degrades to None, never crashes
        logger.warning("tree-sitter grammar for %r unavailable: %s", language, exc)
    _PARSERS[language] = parser
    return parser


def _node_text(node: Any, content: str) -> str:
    return content[node.start_byte : node.end_byte]


def _first_string_child(node: Any, content: str) -> str | None:
    if node is None:
        return None
    for child in node.children:
        if child.type in ("string", "interpreted_string_literal", "raw_string_literal"):
            text = _node_text(child, content).strip("\"'` ")
            for q in ('"""', "'''", "`"):
                text = text.strip(q)
            return text.strip()
        if child.type == "block":
            return _first_string_child(child, content)
    return None


def _module_name_from_path(rel_path: str) -> str:
    parts = list(Path(rel_path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _qualified(module: str, *parts: str | None) -> str:
    return ".".join(s for s in [module, *parts] if s)


def _member_qname(module: str, class_context: str | None, name: str) -> str:
    """Qualified name for a class/function. `class_context` is already module-qualified, so nest
    under it directly (fixes the legacy double-module bug for methods)."""
    return f"{class_context}.{name}" if class_context else _qualified(module, name)


def _collect_python_calls(body_node: Any, content: str) -> list[str]:
    calls: list[str] = []

    def walk(n: Any) -> None:
        if n.type == "call":
            fn = n.child_by_field_name("function")
            if fn:
                calls.append(_node_text(fn, content))
        for ch in n.children:
            walk(ch)

    if body_node is not None:
        walk(body_node)
    return calls


def _collect_python_import(
    node: Any, content: str, file_path: str, symbols: list[RawSymbol]
) -> None:
    line = node.start_point[0] + 1
    is_relative = _node_text(node, content).strip().startswith("from .")
    if node.type == "import_statement":
        for ch in node.children:
            if ch.type in ("dotted_name", "aliased_import"):
                raw = _node_text(ch, content)
                target = raw.split(" as ")[0].strip()
                alias = raw.split(" as ")[-1].strip() if " as " in raw else ""
                symbols.append(
                    RawSymbol(
                        symbol_type="Module",
                        name=target.split(".")[-1],
                        qualified_name=target,
                        language="python",
                        file_path=file_path,
                        start_line=line,
                        end_line=line,
                        raw_imports=[
                            {"target": target, "alias": alias, "line": line, "relative": False}
                        ],
                    )
                )
    elif node.type == "import_from_statement":
        from_name, names = "", []
        for ch in node.children:
            if ch.type == "dotted_name":
                from_name = _node_text(ch, content)
            elif ch.type in ("identifier", "aliased_import"):
                names.append(_node_text(ch, content).split(" as ")[0].strip())
        for n in names or [from_name]:
            target = f"{from_name}.{n}" if from_name and n and n != from_name else (from_name or n)
            symbols.append(
                RawSymbol(
                    symbol_type="Module",
                    name=n,
                    qualified_name=target,
                    language="python",
                    file_path=file_path,
                    start_line=line,
                    end_line=line,
                    raw_imports=[
                        {"target": target, "alias": "", "line": line, "relative": is_relative}
                    ],
                )
            )


_VAR_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^\s=]+)(?:\s*=\s*(.+))?$")


def _collect_python_variable(node, content, meta, module_name, class_context, symbols) -> None:
    m = _VAR_RE.match(_node_text(node, content).strip())
    if not m:
        return
    symbols.append(
        RawSymbol(
            symbol_type="Variable",
            name=m.group(1),
            qualified_name=_qualified(module_name, class_context, m.group(1)),
            language="python",
            file_path=meta.path,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            type_annotation=m.group(2),
            value_preview=(m.group(3) or "")[:200],
            parent_class=class_context,
        )
    )


def _extract_python(tree: Any, content: str, meta: ParsedFile) -> list[RawSymbol]:
    module_name = _module_name_from_path(meta.path)
    symbols: list[RawSymbol] = [
        RawSymbol(
            symbol_type="Module",
            name=module_name.split(".")[-1],
            qualified_name=module_name,
            language="python",
            file_path=meta.path,
            start_line=1,
            end_line=content.count("\n") + 1,
        )
    ]

    def walk(node: Any, class_context: str | None = None) -> None:
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                cls_name = _node_text(name_node, content)
                qname = _member_qname(module_name, class_context, cls_name)
                bases = []
                arg_node = node.child_by_field_name("superclasses")
                if arg_node:
                    bases = [
                        _node_text(ch, content)
                        for ch in arg_node.children
                        if ch.type == "identifier"
                    ]
                body = node.child_by_field_name("body")
                symbols.append(
                    RawSymbol(
                        symbol_type="Class",
                        name=cls_name,
                        qualified_name=qname,
                        language="python",
                        file_path=meta.path,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        docstring=_first_string_child(body or node, content),
                        is_test=meta.is_test,
                        raw_bases=bases,
                    )
                )
                if body:
                    for child in body.children:
                        walk(child, qname)
        elif node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                fn_name = _node_text(name_node, content)
                qname = _member_qname(module_name, class_context, fn_name)
                is_async = any(ch.type == "async" for ch in node.children)
                params = node.child_by_field_name("parameters")
                ret = node.child_by_field_name("return_type")
                sig = _node_text(params, content) if params else ""
                if ret:
                    sig += " -> " + _node_text(ret, content).lstrip("->").strip()
                body = node.child_by_field_name("body")
                symbols.append(
                    RawSymbol(
                        symbol_type="Function",
                        name=fn_name,
                        qualified_name=qname,
                        language="python",
                        file_path=meta.path,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        docstring=_first_string_child(body, content) if body else None,
                        signature=sig,
                        is_async=is_async,
                        is_method=class_context is not None,
                        is_test=meta.is_test or fn_name.startswith("test_"),
                        parent_class=class_context,
                        raw_calls=_collect_python_calls(body, content) if body else [],
                    )
                )
        elif node.type in ("import_statement", "import_from_statement"):
            _collect_python_import(node, content, meta.path, symbols)
        elif node.type in ("expression_statement", "assignment") and class_context is None:
            _collect_python_variable(node, content, meta, module_name, class_context, symbols)
        else:
            for child in node.children:
                walk(child, class_context)

    for child in tree.root_node.children:
        walk(child)
    return symbols


def _extract_ts_js(tree: Any, content: str, meta: ParsedFile) -> list[RawSymbol]:
    module_name = _module_name_from_path(meta.path)
    symbols: list[RawSymbol] = []

    def walk(node: Any, class_context: str | None = None) -> None:
        t = node.type
        if t == "class_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                cls_name = _node_text(name_node, content)
                qname = _member_qname(module_name, class_context, cls_name)
                bases = []
                heritage = node.child_by_field_name("class_heritage")
                if heritage:
                    bases = [
                        _node_text(ch, content)
                        for ch in heritage.children
                        if ch.type == "identifier"
                    ]
                symbols.append(
                    RawSymbol(
                        symbol_type="Class",
                        name=cls_name,
                        qualified_name=qname,
                        language=meta.language,
                        file_path=meta.path,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        is_test=meta.is_test,
                        raw_bases=bases,
                    )
                )
                body = node.child_by_field_name("body")
                if body:
                    for ch in body.children:
                        walk(ch, qname)
        elif t in ("function_declaration", "method_definition", "function"):
            name_node = node.child_by_field_name("name")
            if name_node:
                fn_name = _node_text(name_node, content)
                qname = _member_qname(module_name, class_context, fn_name)
                symbols.append(
                    RawSymbol(
                        symbol_type="Function",
                        name=fn_name,
                        qualified_name=qname,
                        language=meta.language,
                        file_path=meta.path,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        is_async=any(ch.type == "async" for ch in node.children),
                        is_method=class_context is not None,
                        is_test=meta.is_test or fn_name.startswith("test_"),
                        parent_class=class_context,
                    )
                )
        elif t == "import_declaration":
            line = node.start_point[0] + 1
            source = next(
                (
                    _node_text(ch, content).strip("\"'")
                    for ch in node.children
                    if ch.type == "string"
                ),
                None,
            )
            if source:
                symbols.append(
                    RawSymbol(
                        symbol_type="Module",
                        name=source.split("/")[-1],
                        qualified_name=source,
                        language=meta.language,
                        file_path=meta.path,
                        start_line=line,
                        end_line=line,
                        raw_imports=[
                            {
                                "target": source,
                                "alias": "",
                                "line": line,
                                "relative": source.startswith("."),
                            }
                        ],
                    )
                )
        else:
            for ch in node.children:
                walk(ch, class_context)

    for ch in tree.root_node.children:
        walk(ch)
    return symbols


_EXTRACTORS = {
    "python": _extract_python,
    "typescript": _extract_ts_js,
    "javascript": _extract_ts_js,
}


def language_for(path: str) -> str | None:
    return LANGUAGE_EXTENSIONS.get(Path(path).suffix.lower())


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    return any(p.startswith("test") or p in ("tests", "__tests__") for p in parts)


def parse_source(path: str, data: bytes) -> tuple[ParsedFile, list[RawSymbol]] | None:
    """Parse one source file's bytes -> (ParsedFile, symbols). None if language unsupported."""
    language = language_for(path)
    if language is None:
        return None
    meta = ParsedFile(
        path=path,
        language=language,
        size_bytes=len(data),
        content_hash=hashlib.sha256(data).hexdigest(),
        is_test=_is_test_path(path),
    )
    parser = _get_parser(language)
    extractor = _EXTRACTORS.get(language)
    if parser is None or extractor is None:
        return meta, []  # grammar/extractor absent -> file node only, zero symbols (logged)
    try:
        tree = parser.parse(data)
        return meta, extractor(tree, data.decode("utf-8", "replace"), meta)
    except Exception as exc:  # noqa: BLE001 — a bad file never kills the run
        logger.warning("parse failed for %s: %s", path, exc)
        return meta, []


def resolve_edges(symbols: list[RawSymbol]) -> tuple[list[dict], list[dict], list[dict]]:
    """Resolve CALLS / IMPORTS / INHERITS edges across the symbol table (by qualified name)."""
    # CALLS/INHERITS resolve against functions+classes. "internal" for an import means the target is
    # actually defined in this codebase (a file module, or a function/class qname) — NOT just an
    # import marker (those also become Module symbols, so excluding them avoids false-internal).
    sym_table = {s.qualified_name: s for s in symbols if s.symbol_type in ("Function", "Class")}
    file_modules = {
        s.qualified_name for s in symbols if s.symbol_type == "Module" and not s.raw_imports
    }
    defined = set(sym_table) | file_modules
    calls, imports, inherits = [], [], []
    for sym in symbols:
        if sym.symbol_type == "Function":
            for raw_call in sym.raw_calls:
                callee = sym_table.get(raw_call)
                if callee is None or callee.symbol_type != "Function":
                    callee = next(
                        (
                            s
                            for qn, s in sym_table.items()
                            if qn.endswith("." + raw_call) and s.symbol_type == "Function"
                        ),
                        None,
                    )
                if callee:
                    calls.append({"caller": sym.qualified_name, "callee": callee.qualified_name})
        for imp in sym.raw_imports:
            imports.append(
                {
                    "source_file": sym.file_path,
                    "target": imp["target"],
                    "is_internal": imp["target"] in defined,
                }
            )
        if sym.symbol_type == "Class":
            for base_name in sym.raw_bases:
                parent = sym_table.get(base_name) or next(
                    (
                        s
                        for qn, s in sym_table.items()
                        if qn.endswith("." + base_name) and s.symbol_type == "Class"
                    ),
                    None,
                )
                if parent:
                    inherits.append({"child": sym.qualified_name, "parent": parent.qualified_name})
    return calls, imports, inherits
