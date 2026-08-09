"""The shared minting function — one function, over a ``SourceRef`` (§CITE rev2/rev3).

A citation is minted **once, in platform code, at the tool-execution boundary**. Three paths reach
that boundary — a connector read that goes on to ingest, a connector read used directly in a run,
and a live web or MCP read — and §CITE requires them to share one minting function. So this is a
general function over a ``SourceRef``, not an ingest helper the other paths borrow: rows two and
three have no stored record to stamp, and an ingest-shaped function would have to be rewritten for
them.

The model is never in this path. It receives citations as a reserved result key it cannot write.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from oraclous_citation.identity import compute_citation_id
from oraclous_citation.models import Citation, SourceRef


def mint_citation(
    source: SourceRef,
    *,
    content: str | None = None,
    retrieved_at: datetime | None = None,
) -> Citation:
    """Mint the complete ``Citation`` for one read of ``source``.

    ``revision`` is never null outbound. When the source exposes no version of its own, the
    platform falls back to the SHA-256 of the content it holds, with ``revision_kind`` set to
    ``content_hash`` — so a source with no version concept still supersedes correctly on a content
    change. ``retrieved_at`` is server-computed and sits outside the identity: two reads of the
    same unchanged revision are the same record, minutes apart.

    Raises ``ValueError`` when neither a revision nor the content is available. Minting silently
    with an empty or invented revision would forge exactly the provenance this Contract exists to
    prevent, so this fails closed instead.
    """
    revision = source.revision
    revision_kind: str | None = source.revision_kind
    if revision is None:
        if content is None:
            raise ValueError(
                "cannot mint a citation: the source carries no revision and no content was "
                "supplied to hash as a fallback"
            )
        revision = hashlib.sha256(content.encode("utf-8")).hexdigest()
        revision_kind = "content_hash"

    return Citation.model_validate(
        {
            "citation_id": compute_citation_id(source.source_system, source.source_id, revision),
            "source_system": source.source_system,
            "source_id": source.source_id,
            "revision": revision,
            "revision_kind": revision_kind,
            "url": source.url,
            "title": source.title,
            "author": source.author,
            "retrieved_at": retrieved_at if retrieved_at is not None else datetime.now(UTC),
            "permission_ref": source.permission_ref,
        }
    )
