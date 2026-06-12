"""Vision (image → text) extraction seam (ORAA-4 §21 services layer).

Reshaped from legacy `develop@84152635 knowledge-graph-builder/app/services/vision_extractor.py`.
The legacy extractor called the Anthropic + OpenAI SDKs directly (two hardcoded vendor paths, an
`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` fork). This reshape collapses both onto the platform's SINGLE
live LLM seam — the same OpenAI-compatible OpenRouter config the entity extractor uses
(`KGS_EXTRACTOR=openai`, `KGS_OPENAI_API_KEY`, `KGS_OPENAI_BASE_URL`, a `<provider>/<model>` id) —
so vision goes through one endpoint and one key, no per-vendor branch.

It asks a vision-capable chat model to read an image and emit a strict entities/relationships JSON,
then serialises that JSON to plain prose (`to_text`). That prose feeds the EXISTING text ingest path
(document → text → chunk → embed → entity-extract → write) exactly like a `.txt`/`.md`/`.pdf` body —
no parallel pipeline. A technical-diagram hint switches to a nodes/edges prompt (still serialised to
prose), preserving the legacy diagram mode without a second graph-write path.

Fail-closed: with no LLM configured (`KGS_EXTRACTOR=null` or no key), `make_vision_extractor`
returns None and the document extractor raises `ExtractionError` for an image — it never silently
drops an image to empty text. The chat client is injectable, so tests exercise the prompt/serialise
with a fake (no network).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Protocol, runtime_checkable

from oraclous_knowledge_graph_service.core.config import Settings

logger = logging.getLogger(__name__)

# Image media types this seam knows how to send (SVG is sent as PNG to the vision model).
_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "svg": "image/png",
}

# Filename stems that suggest a technical diagram (architecture/UML/flowchart/ER).
_DIAGRAM_FILENAME_KEYWORDS = {"arch", "diagram", "flow", "uml", "schema", "model"}

_EXTRACTION_PROMPT = """\
Analyze this image and extract all entities and relationships visible in it.

Return ONLY valid JSON matching this exact schema -- no prose, no markdown fences:
{
  "entities": [
    {"name": "string", "type": "string", "description": "string"}
  ],
  "relationships": [
    {"source": "string", "target": "string", "type": "string", "description": "string"}
  ]
}

Rules:
- Entity types are singular nouns (Person, Organization, System, Service, Concept, etc.)
- Relationship types are UPPER_SNAKE_CASE verbs (WORKS_FOR, DEPENDS_ON, CALLS, CONTAINS, etc.)
- Extract ALL components and connections visible -- be exhaustive for diagrams
- Do not invent information not visible in the image
- If nothing can be extracted return {"entities": [], "relationships": []}

Context: %(context)s
"""

_DIAGRAM_PROMPT = """\
This image appears to be a technical diagram (architecture, UML, flowchart, or similar).
Extract the components and their relationships as ONLY valid JSON -- no prose, no markdown fences:
{
  "nodes": [{"id": "string", "label": "string", "type": "component|service|database|process"}],
  "edges": [{"from": "string", "to": "string", "label": "string", "type": "connection|data_flow"}],
  "diagram_type": "architecture|uml_class|uml_sequence|flowchart|er|other",
  "description": "one-sentence summary"
}
node "type" and edge "type" are free strings -- the examples above are the common values.
If you cannot identify structured components return
{"nodes": [], "edges": [], "diagram_type": "other", "description": "..."}.

Context: %(context)s
"""


@runtime_checkable
class VisionChatClient(Protocol):
    """The minimal vision-chat surface the extractor needs (one image + one text prompt → text).

    The real implementation wraps the OpenAI-compatible client pointed at OpenRouter; tests inject a
    fake that returns a fixed JSON string with no network.
    """

    def complete(self, *, prompt: str, image_b64: str, media_type: str) -> str: ...


class OpenAIVisionChatClient:
    """Vision chat via the OpenAI-compatible Chat Completions API (OpenRouter base by default).

    Sends the image as a `data:` URL part plus the text prompt; returns the raw model text. Lazy
    `openai` import so the key-free (`null`) path never imports it.
    """

    def __init__(self, *, api_key: str, model: str, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url

    def complete(self, *, prompt: str, image_b64: str, media_type: str) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        response = client.chat.completions.create(
            model=self._model,
            max_tokens=4096,
            temperature=0.0,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        return response.choices[0].message.content or ""


def is_image_ext(ext: str) -> bool:
    """True when a file extension (lower-case, no dot) is a supported image type."""
    return ext in _MEDIA_TYPES


def _media_type(ext: str) -> str:
    return _MEDIA_TYPES.get(ext, "image/png")


def _looks_like_diagram(filename: str | None) -> bool:
    if not filename:
        return False
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    return any(kw in stem for kw in _DIAGRAM_FILENAME_KEYWORDS)


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        inner = lines[1:-1] if stripped.endswith("```") else lines[1:]
        stripped = "\n".join(inner)
    return stripped.strip()


def _parse_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(_strip_fences(text))
    except json.JSONDecodeError as exc:
        logger.warning("vision: could not parse model JSON (%s): %.200r", exc, text)
        return {}


def entities_to_text(result: dict[str, Any], context: str = "") -> str:
    """Serialise an entities/relationships dict to prose for the text ingest path.

    e.g. ``Lambda is a Service. Serverless compute.`` / ``Lambda depends on API Gateway.`` — the
    SAME free-text shape `extract_text` returns for any other document, so the downstream chunk →
    embed → entity-extract spine treats a vision result identically to a text body.
    """
    lines: list[str] = []
    if context:
        lines.append(f"Source context: {context}")
    for entity in result.get("entities", []):
        name = (entity.get("name") or "").strip()
        if not name:
            continue
        etype = (entity.get("type") or "Entity").strip()
        desc = (entity.get("description") or "").strip()
        line = f"{name} is a {etype}."
        if desc:
            line += f" {desc}"
        lines.append(line)
    for rel in result.get("relationships", []):
        src = (rel.get("source") or "").strip()
        tgt = (rel.get("target") or "").strip()
        if not src or not tgt:
            continue
        rtype = (rel.get("type") or "RELATED_TO").replace("_", " ").strip().lower()
        desc = (rel.get("description") or "").strip()
        line = f"{src} {rtype} {tgt}."
        if desc:
            line += f" Context: {desc}"
        lines.append(line)
    return "\n".join(lines)


def diagram_to_text(result: dict[str, Any], context: str = "") -> str:
    """Serialise a diagram nodes/edges dict to the same entities/relationships prose shape."""
    entities = [
        {"name": n.get("label") or n.get("id"), "type": n.get("type", "component")}
        for n in result.get("nodes", [])
    ]
    relationships = [
        {
            "source": _node_label(result, e.get("from")),
            "target": _node_label(result, e.get("to")),
            "type": (e.get("label") or e.get("type") or "CONNECTED_TO"),
        }
        for e in result.get("edges", [])
    ]
    summary = (result.get("description") or "").strip()
    ctx = f"{context}. {summary}".strip(". ") if (context or summary) else ""
    return entities_to_text({"entities": entities, "relationships": relationships}, context=ctx)


def _node_label(result: dict[str, Any], node_id: Any) -> str:
    for node in result.get("nodes", []):
        if node.get("id") == node_id:
            return str(node.get("label") or node.get("id") or "")
    return str(node_id or "")


class VisionExtractor:
    """Image bytes → prose (via an injectable vision chat client).

    `extract(data, filename, context)` returns `(text, metadata)` matching the document-extractor
    contract: prose suitable for the existing text ingest path, plus a `{kind: image, mode, ...}`
    metadata sidecar. A diagram-shaped image (filename hint) uses the nodes/edges prompt; both modes
    serialise to the same prose so there is one downstream path.
    """

    def __init__(self, *, client: VisionChatClient) -> None:
        self._client = client

    def extract(
        self, *, data: bytes, filename: str | None = None, context: str = ""
    ) -> tuple[str, dict[str, Any]]:
        ext = (filename or "").rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
        media_type = _media_type(ext)
        image_b64 = base64.standard_b64encode(data).decode("utf-8")
        diagram = _looks_like_diagram(filename)
        prompt = (_DIAGRAM_PROMPT if diagram else _EXTRACTION_PROMPT) % {
            "context": context or "No additional context provided."
        }
        raw = self._client.complete(prompt=prompt, image_b64=image_b64, media_type=media_type)
        parsed = _parse_json(raw)
        if diagram:
            mode = "diagram"
            text = diagram_to_text(parsed, context=context)
            entity_count = len(parsed.get("nodes", []))
            rel_count = len(parsed.get("edges", []))
        else:
            mode = "entities"
            text = entities_to_text(parsed, context=context)
            entity_count = len(parsed.get("entities", []))
            rel_count = len(parsed.get("relationships", []))
        metadata: dict[str, Any] = {
            "kind": "image",
            "mode": mode,
            "media_type": media_type,
            "vision_entities": entity_count,
            "vision_relationships": rel_count,
        }
        return text, metadata


def make_vision_extractor(settings: Settings) -> VisionExtractor | None:
    """Build the vision extractor from config, or None when the LLM seam is off.

    Gated by the SAME switch as free-text entity extraction (`KGS_EXTRACTOR`): vision needs the live
    LLM, so `null` mode (the key-free CI default) has no vision and an image upload fails closed in
    the document extractor. `openai` mode with no key raises (never a silent no-op).
    """
    if settings.extractor == "null":
        return None
    if not settings.openai_api_key:
        raise RuntimeError("KGS_EXTRACTOR=openai requires KGS_OPENAI_API_KEY for vision extraction")
    client = OpenAIVisionChatClient(
        api_key=settings.openai_api_key,
        model=settings.extractor_model,
        base_url=settings.openai_base_url,
    )
    return VisionExtractor(client=client)
