"""File → text extractors (ORAA-4 §21 services layer).

Reshaped from legacy `develop@84152635 knowledge-graph-builder/app/services/{document_processor,
pdf_extractor,md_extractor}.py`. Key-free and deterministic:
  - text : UTF-8 decode (zero deps)
  - md   : UTF-8 decode; text is the RAW markdown, plus a structured {title, sections, hierarchy}
           sidecar from the stdlib-`re` heading parser (lifted verbatim — no markdown lib)
  - pdf  : `pypdf` text-only ("[Page N]\\n…" joined by blank lines). No OCR / images / vision.
  - docx : `python-docx` paragraphs + table cells.
Raises plain `ExtractionError` (the route layer maps it to HTTP) — no FastAPI coupling in services.
"""

from __future__ import annotations

import io
import re
from typing import Any

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_TEXT_EXTS = {"txt", "text", "log", ""}
_MD_EXTS = {"md", "markdown", "mdown"}
# Structured kinds (csv/tsv/json/jsonl) are recognised by _kind so /upload validates + stores the
# right source_type, but extract_text never handles them — the worker routes structured sources to
# StructuredIngestionService instead.


class ExtractionError(Exception):
    """Extraction failed (unsupported type, corrupt file, or empty text). Maps to HTTP 422."""


def _ext(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def _kind(source_type: str | None, filename: str | None) -> str:
    st = (source_type or "").lower()
    if st in {"text", "txt", "md", "markdown", "pdf", "docx", "doc"}:
        return "md" if st in {"md", "markdown"} else ("docx" if st in {"docx", "doc"} else st)
    if st in {"csv", "tsv", "json", "jsonl"}:
        return st
    ext = _ext(filename)
    if ext == "pdf":
        return "pdf"
    if ext in {"docx", "doc"}:
        return "docx"
    if ext in {"csv", "tsv", "json", "jsonl"}:
        return ext
    if ext in _MD_EXTS:
        return "md"
    if ext in _TEXT_EXTS:
        return "text"
    raise ExtractionError(f"unsupported source type: {source_type or filename!r}")


def _build_hierarchy(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = []
    stack: list[dict[str, Any]] = []
    for sec in sections:
        node = {"level": sec["level"], "heading": sec["heading"], "children": []}
        while stack and stack[-1]["level"] >= node["level"]:
            stack.pop()
        if stack:
            stack[-1]["children"].append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def _extract_markdown(text: str, fallback_title: str) -> dict[str, Any]:
    matches = list(_HEADING_RE.finditer(text))
    sections: list[dict[str, Any]] = []
    for i, match in enumerate(matches):
        level = len(match.group(1))
        heading = match.group(2).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append({"level": level, "heading": heading, "content": text[start:end].strip()})
    title = next((s["heading"] for s in sections if s["level"] == 1), fallback_title)
    return {"title": title, "sections": sections, "hierarchy": _build_hierarchy(sections)}


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dep is declared
        raise ExtractionError("pypdf is required to extract PDF files") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(f"could not read PDF: {exc}") from exc
    parts: list[str] = []
    for i, page in enumerate(reader.pages):
        page_text = (page.extract_text() or "").strip()
        if page_text:
            parts.append(f"[Page {i + 1}]\n{page_text}")
    return "\n\n".join(parts)


def _extract_docx(data: bytes) -> str:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - dep is declared
        raise ExtractionError("python-docx is required to extract DOCX files") from exc
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ExtractionError(f"could not read DOCX: {exc}") from exc
    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        rows = ["\t".join(c.text.strip() for c in row.cells) for row in table.rows]
        rows = [r for r in rows if r.strip()]
        if rows:
            parts.append("[Table]\n" + "\n".join(rows))
    return "\n\n".join(parts)


def source_type_for(filename: str | None) -> str:
    """Resolve + validate the source_type for an upload (raises ExtractionError if unsupported)."""
    return _kind(None, filename)


def extract_text(
    *, data: bytes, filename: str | None = None, source_type: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Return (plain_text, metadata). Raises ExtractionError on unsupported/empty input."""
    kind = _kind(source_type, filename)
    metadata: dict[str, Any] = {"kind": kind, "filename": filename}
    if kind == "pdf":
        text = _extract_pdf(data)
    elif kind == "docx":
        text = _extract_docx(data)
    else:
        text = data.decode("utf-8", errors="replace")
        if kind == "md":
            metadata["structured"] = _extract_markdown(text, fallback_title=filename or "")
    text = text.strip()
    if not text:
        raise ExtractionError("no extractable text found")
    return text, metadata
