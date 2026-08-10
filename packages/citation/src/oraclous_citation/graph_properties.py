"""The graph-store encoding: flatten a citation onto a node, and lift it back.

A Neo4j property is a primitive, so a nested ``citation`` map cannot sit on a node. §CITE leaves
the encoding to the implementing brief ("mechanism, not shape"), but it cannot be left to each
service: ``knowledge-graph-service`` flattens on the write path and ``knowledge-retriever-service``
lifts on the read path, they live in different services, and they must agree exactly. So the pair
lives here.

Three decisions worth stating, because each of them is load-bearing:

**Flat scalars, one property per field — never a JSON blob.** A blob would round-trip just as well
and would leave ``citation_id`` unfilterable in Cypher, which the served-set work and any future
"find every record from this source" query needs.

**The empty string encodes a null.** Neo4j DELETES a property set to ``null``, so an explicit null
and a truncated record would be indistinguishable on read. Every nullable citation field is
``minLength: 1`` in the shared schema, so ``""`` can never be a legitimate value and is unambiguous.

**Lifting is all-or-nothing.** Every property must be present, or the node lifts to ``None``. A
citation is never partly filled: a node carrying only some of the properties — a partial write, a
hand-edited node — resolves to nothing rather than to a citation with holes in it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from oraclous_citation.models import Author, Citation

#: Every property a stored citation occupies. The prefix is what the write path strips (so a
#: caller-planted citation never survives) and what the read path lifts out of the property bag.
CITATION_PROPERTY_PREFIX = "citation"

_ID = "citation_id"
_SOURCE_SYSTEM = "citation_source_system"
_SOURCE_ID = "citation_source_id"
_REVISION = "citation_revision"
_REVISION_KIND = "citation_revision_kind"
_URL = "citation_url"
_TITLE = "citation_title"
_RETRIEVED_AT = "citation_retrieved_at"
_PERMISSION_REF = "citation_permission_ref"
_AUTHOR_DISPLAY_NAME = "citation_author_display_name"
_AUTHOR_SOURCE_HANDLE = "citation_author_source_handle"
_AUTHOR_EMAIL = "citation_author_email"

_KEYS = (
    _ID,
    _SOURCE_SYSTEM,
    _SOURCE_ID,
    _REVISION,
    _REVISION_KIND,
    _URL,
    _TITLE,
    _RETRIEVED_AT,
    _PERMISSION_REF,
    _AUTHOR_DISPLAY_NAME,
    _AUTHOR_SOURCE_HANDLE,
    _AUTHOR_EMAIL,
)

#: The sentinel for "the source exposes none of this". See the module docstring.
_ABSENT = ""


def _as_stored(value: str | None) -> str:
    return value if value is not None else _ABSENT


def _as_value(stored: str) -> str | None:
    return stored if stored != _ABSENT else None


def citation_properties(citation: Citation) -> dict[str, str]:
    """Flatten ``citation`` into Neo4j-storable node properties."""
    author = citation.author
    return {
        _ID: citation.citation_id,
        _SOURCE_SYSTEM: citation.source_system,
        _SOURCE_ID: citation.source_id,
        _REVISION: citation.revision,
        _REVISION_KIND: citation.revision_kind,
        _URL: _as_stored(citation.url),
        _TITLE: _as_stored(citation.title),
        _RETRIEVED_AT: citation.retrieved_at.isoformat().replace("+00:00", "Z"),
        _PERMISSION_REF: _as_stored(citation.permission_ref),
        _AUTHOR_DISPLAY_NAME: _as_stored(author.display_name if author else None),
        _AUTHOR_SOURCE_HANDLE: _as_stored(author.source_handle if author else None),
        _AUTHOR_EMAIL: _as_stored(author.email if author else None),
    }


def citation_from_properties(properties: Mapping[str, Any] | None) -> Citation | None:
    """Lift a citation back out of a stored node's properties, ignoring everything else.

    Returns ``None`` for a record with no source identity — one ingested before this Contract, or
    ingested without a ``source`` — and for any record whose citation properties are incomplete.
    """
    if not properties:
        return None

    stored: dict[str, str] = {}
    for key in _KEYS:
        value = properties.get(key)
        if not isinstance(value, str):
            return None
        stored[key] = value

    display_name = _as_value(stored[_AUTHOR_DISPLAY_NAME])
    author = (
        Author(
            display_name=display_name,
            source_handle=_as_value(stored[_AUTHOR_SOURCE_HANDLE]),
            email=_as_value(stored[_AUTHOR_EMAIL]),
        )
        if display_name is not None
        else None
    )

    try:
        return Citation.model_validate(
            {
                "citation_id": stored[_ID],
                "source_system": stored[_SOURCE_SYSTEM],
                "source_id": stored[_SOURCE_ID],
                "revision": stored[_REVISION],
                "revision_kind": stored[_REVISION_KIND],
                "url": _as_value(stored[_URL]),
                "title": _as_value(stored[_TITLE]),
                "author": author,
                "retrieved_at": stored[_RETRIEVED_AT],
                "permission_ref": _as_value(stored[_PERMISSION_REF]),
            }
        )
    except ValidationError:
        return None


def is_citation_property(key: str) -> bool:
    """Whether ``key`` is one this encoding owns — the strip predicate both services share.

    Deliberately broader than the exact key set: a bare ``citation`` key, and anything else under
    the prefix, is caller-planted provenance and must not survive a write or reach a reader.
    """
    return key == CITATION_PROPERTY_PREFIX or key.startswith(f"{CITATION_PROPERTY_PREFIX}_")
