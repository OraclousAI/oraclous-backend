"""Unit tests for the first-class document extractors + the pluggable dispatch (#306, refs #294).

Restores PDF (text + pdfplumber tables) / vision (image -> prose via the live LLM seam) / Markdown /
richer text extraction as per-content-type extractors dispatched by source_type/extension. All
deterministic + key-free: the PDF/DOCX parsers and the vision LLM are faked (no network, no real
binaries), exercising the dispatch + serialisation logic the recipe/ingest path depends on.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.services import extractors
from oraclous_knowledge_graph_service.services.extractors import (
    ExtractionError,
    extract_text,
    register_extractor,
    source_type_for,
    supported_kinds,
)
from oraclous_knowledge_graph_service.services.vision_extractor import (
    VisionExtractor,
    diagram_to_text,
    entities_to_text,
    make_vision_extractor,
)

pytestmark = pytest.mark.unit


# --- dispatch / source_type resolution ---------------------------------------
def test_source_type_for_resolves_each_first_class_type() -> None:
    assert source_type_for("a.pdf") == "pdf"
    assert source_type_for("a.md") == "md"
    assert source_type_for("a.txt") == "text"
    assert source_type_for("README") == "text"
    assert source_type_for("a.docx") == "docx"
    assert source_type_for("a.png") == "image"
    assert source_type_for("photo.JPEG") == "image"


def test_unsupported_extension_raises() -> None:
    with pytest.raises(ExtractionError):
        source_type_for("a.exe")


def test_registry_lists_first_class_kinds() -> None:
    kinds = supported_kinds()
    for kind in ("text", "md", "pdf", "docx", "image"):
        assert kind in kinds


def test_structured_kind_has_no_text_extractor() -> None:
    # csv/json resolve to a kind (for /upload validation) but are routed to the structured path —
    # a direct extract_text on one must fail loudly, never silently produce empty text.
    with pytest.raises(ExtractionError):
        extract_text(data=b"a,b\n1,2", filename="x.csv", source_type="csv")


def test_register_extractor_plugs_in_a_new_type() -> None:
    def fake(*, data: bytes, **_: Any) -> tuple[str, dict[str, Any]]:
        return data.decode().upper(), {"kind": "shout"}

    # Override the built-in "text" extractor to prove dispatch picks the registered callable.
    register_extractor("text", fake)
    try:
        text, meta = extract_text(data=b"hi", filename="a.txt", source_type="text")
        assert text == "HI"
        assert meta["kind"] == "shout"
    finally:
        # restore the built-in text extractor so other tests are unaffected
        register_extractor("text", extractors._extract_text)


# --- text / markdown ----------------------------------------------------------
def test_extract_plain_text() -> None:
    text, meta = extract_text(data=b"hello\n\nworld", filename="a.txt", source_type="text")
    assert "hello" in text and "world" in text
    assert meta["kind"] == "text"


def test_extract_markdown_structured_sidecar() -> None:
    text, meta = extract_text(
        data=b"# Title\n\nbody\n\n## Sub\n\nmore", filename="n.md", source_type="md"
    )
    assert text.startswith("# Title")  # md text is the RAW markdown
    assert meta["structured"]["title"] == "Title"
    headings = [s["heading"] for s in meta["structured"]["sections"]]
    assert headings == ["Title", "Sub"]


def test_empty_text_raises() -> None:
    with pytest.raises(ExtractionError):
        extract_text(data=b"   \n\n  ", filename="a.txt", source_type="text")


# --- PDF (faked pypdf + pdfplumber) ------------------------------------------
class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakeReader:
    def __init__(self, _stream: Any) -> None:
        self.pages = [_FakePage("First page body."), _FakePage("Second page body.")]


class _FakePlumberPage:
    def extract_tables(self) -> list[list[list[str]]]:
        return [[["h1", "h2"], ["a", "b"]]]


class _FakePlumberPdf:
    pages = [_FakePlumberPage()]

    def __enter__(self) -> _FakePlumberPdf:
        return self

    def __exit__(self, *_: Any) -> None:
        return None


def test_pdf_extracts_per_page_text_and_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pypdf.PdfReader", _FakeReader)
    fake_plumber = types.ModuleType("pdfplumber")
    fake_plumber.open = lambda _stream: _FakePlumberPdf()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_plumber)

    text, meta = extract_text(data=b"%PDF-fake", filename="doc.pdf", source_type="pdf")
    assert "[Page 1]\nFirst page body." in text
    assert "[Page 2]\nSecond page body." in text
    assert "[Table]\nh1\th2\na\tb" in text
    assert meta["kind"] == "pdf"
    assert meta["page_count"] == 2
    assert meta["has_tables"] is True


class _FakePlumberNoTablesPage:
    def extract_tables(self) -> list[Any]:
        return []


class _FakePlumberNoTablesPdf:
    pages = [_FakePlumberNoTablesPage()]

    def __enter__(self) -> _FakePlumberNoTablesPdf:
        return self

    def __exit__(self, *_: Any) -> None:
        return None


def test_pdf_without_tables_still_extracts_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pypdf.PdfReader", _FakeReader)
    fake_plumber = types.ModuleType("pdfplumber")
    fake_plumber.open = lambda _stream: _FakePlumberNoTablesPdf()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_plumber)

    text, meta = extract_text(data=b"%PDF-fake", filename="doc.pdf", source_type="pdf")
    assert "First page body." in text
    assert "[Table]" not in text
    assert meta["has_tables"] is False


# --- vision (image -> prose via a fake LLM client) ---------------------------
class _FakeVisionClient:
    """Returns a fixed JSON string; records the prompt so the diagram-mode switch is observable."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.last_prompt: str | None = None

    def complete(self, *, prompt: str, image_b64: str, media_type: str) -> str:
        self.last_prompt = prompt
        assert image_b64  # the image was base64-encoded and passed through
        return self.payload


_ENTITIES_JSON = """```json
{"entities": [{"name": "Lambda", "type": "Service", "description": "Serverless compute."}],
 "relationships": [{"source": "Lambda", "target": "API Gateway", "type": "DEPENDS_ON",
                    "description": "invoked via"}]}
```"""


def test_vision_entities_serialise_to_prose() -> None:
    client = _FakeVisionClient(_ENTITIES_JSON)
    extractor = VisionExtractor(client=client)
    text, meta = extractor.extract(data=b"\x89PNG-bytes", filename="screenshot.png")
    assert "Lambda is a Service. Serverless compute." in text
    assert "Lambda depends on API Gateway." in text
    assert meta["kind"] == "image"
    assert meta["mode"] == "entities"
    assert meta["vision_entities"] == 1
    assert meta["vision_relationships"] == 1
    assert "entities and relationships" in (client.last_prompt or "")


_DIAGRAM_JSON = (
    '{"nodes": [{"id": "n1", "label": "Web", "type": "service"},'
    ' {"id": "n2", "label": "DB", "type": "database"}],'
    ' "edges": [{"from": "n1", "to": "n2", "label": "READS_FROM", "type": "data_flow"}],'
    ' "diagram_type": "architecture", "description": "web reads the db"}'
)


def test_vision_diagram_mode_by_filename_hint() -> None:
    client = _FakeVisionClient(_DIAGRAM_JSON)
    extractor = VisionExtractor(client=client)
    text, meta = extractor.extract(data=b"img", filename="architecture_diagram.png")
    assert meta["mode"] == "diagram"
    assert "technical diagram" in (client.last_prompt or "")
    # edges are serialised through node labels, not raw ids
    assert "Web reads from DB." in text
    assert "n1" not in text


def test_vision_bad_json_yields_empty_prose() -> None:
    client = _FakeVisionClient("not json at all")
    extractor = VisionExtractor(client=client)
    text, meta = extractor.extract(data=b"img", filename="x.png")
    assert text == ""
    assert meta["vision_entities"] == 0


def test_entities_to_text_skips_nameless_and_dangling() -> None:
    out = entities_to_text(
        {
            "entities": [{"name": "", "type": "X"}, {"name": "A", "type": "Thing"}],
            "relationships": [{"source": "A", "target": "", "type": "X"}],
        }
    )
    assert out == "A is a Thing."


def test_diagram_to_text_resolves_ids_to_labels() -> None:
    out = diagram_to_text(
        {
            "nodes": [{"id": "1", "label": "Alpha", "type": "service"}],
            "edges": [{"from": "1", "to": "1", "label": "LOOPS"}],
        }
    )
    assert "Alpha is a service." in out
    assert "Alpha loops Alpha." in out


# --- image dispatch through extract_text + fail-closed ------------------------
def test_image_dispatch_uses_vision_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeVisionClient(_ENTITIES_JSON)
    monkeypatch.setattr(
        extractors, "make_vision_extractor", lambda _s: VisionExtractor(client=client)
    )
    text, meta = extract_text(
        data=b"\x89PNG", filename="screenshot.png", source_type="image", settings=Settings()
    )
    assert "Lambda is a Service" in text
    assert meta["kind"] == "image"


def test_image_fails_closed_without_llm() -> None:
    # default Settings -> KGS_EXTRACTOR=null -> no vision extractor -> a clear 422-mapped error,
    # never a silent empty extraction.
    settings = Settings()
    assert settings.extractor == "null"
    with pytest.raises(ExtractionError, match="image extraction requires the LLM seam"):
        extract_text(data=b"img", filename="a.png", source_type="image", settings=settings)


def test_make_vision_extractor_off_in_null_mode() -> None:
    assert make_vision_extractor(Settings()) is None


def test_make_vision_extractor_requires_key() -> None:
    with pytest.raises(RuntimeError, match="KGS_OPENAI_API_KEY"):
        make_vision_extractor(Settings(extractor="openai"))
