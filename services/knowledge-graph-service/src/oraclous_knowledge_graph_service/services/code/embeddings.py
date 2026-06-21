"""Stage 4 — code-symbol embedding generation (services layer — no DB).

Faithful lift-and-reshape of legacy `develop@84152635 code_parser_service.generate_embeddings`
(Stage 4): embed every Function + Class symbol from the qualified name + signature + docstring,
through the SAME OpenAI-compatible embedder the rest of the KGS uses (`make_embedder`), at the
KGS embedding dim. Returns rows the writer sets the `embedding` property from.

FAIL-SOFT (the load-bearing invariant, #305): when the configured embedder is `openai` but no API
key is set, embedding is SKIPPED (empty result) rather than crashing the ingest — the code graph
still writes, only without vectors. Any embed() error is likewise swallowed. The key-free hashing
embedder always succeeds, so the dev/CI path needs no key.
"""

from __future__ import annotations

import logging

from oraclous_knowledge_graph_service.core.config import Settings
from oraclous_knowledge_graph_service.services.embedder import Embedder, make_embedder

logger = logging.getLogger(__name__)

_EMBEDDABLE = ("Function", "Class")


def _embed_text(symbol: dict) -> str:
    """Build the text embedded for one symbol: qualified_name + signature + docstring."""
    props = symbol.get("properties", {})
    parts = [symbol["qualified_name"], props.get("signature") or "", props.get("docstring") or ""]
    return "\n".join(p for p in parts if p).strip()


def make_optional_embedder(settings: Settings) -> Embedder | None:
    """Build the embedder, fail-soft: None when `openai` mode has no key (skip embeddings)."""
    if settings.embedder == "openai" and not settings.openai_api_key:
        logger.warning("KGS_EMBEDDER=openai but no KGS_OPENAI_API_KEY — skipping code embeddings")
        return None
    try:
        return make_embedder(settings)
    except Exception as exc:  # noqa: BLE001 — embedder init never crashes the ingest
        logger.warning("code embedder unavailable, skipping embeddings: %s", exc)
        return None


def generate_embeddings(
    node_symbols: list[dict], embedder: Embedder | None, *, max_symbols: int = 0
) -> list[dict]:
    """Return ``[{"label", "qualified_name", "embedding"}]`` for Function/Class symbols.

    Empty (skip) when there is no embedder. ``max_symbols`` (0 = unbounded) caps how many symbols
    are embedded per ingest: beyond it the overflow is SKIPPED (a structured warning is logged)
    rather than building an unbounded in-memory text list + unbounded embedding calls — a cost/
    memory guard (embeddings are fail-soft, so dropping the overflow only loses vectors, not graph).
    Batched through the embedder's own batching; any embed error is swallowed (fail-soft) and yields
    no rows."""
    if embedder is None:
        return []
    embeddable = [s for s in node_symbols if s["label"] in _EMBEDDABLE]
    if not embeddable:
        return []
    if max_symbols and len(embeddable) > max_symbols:
        logger.warning(
            "code embedding capped: %d embeddable symbols > KGS_CODE_MAX_EMBED_SYMBOLS=%d; "
            "skipping the %d overflow (vectors only, graph is unaffected)",
            len(embeddable),
            max_symbols,
            len(embeddable) - max_symbols,
        )
        embeddable = embeddable[:max_symbols]
    texts = [_embed_text(s) for s in embeddable]
    try:
        vectors = embedder.embed(texts)
        # zip(strict=True) INSIDE the try so an embedder returning a mismatched vector count fails
        # soft (skip embeddings) instead of crashing the whole ingest.
        rows = [
            {"label": s["label"], "qualified_name": s["qualified_name"], "embedding": vec}
            for s, vec in zip(embeddable, vectors, strict=True)
        ]
    except Exception as exc:  # noqa: BLE001 — a failed/mismatched embed skips Stage 4, never ingest
        logger.warning("code embedding failed, skipping: %s", exc)
        return []
    return rows
