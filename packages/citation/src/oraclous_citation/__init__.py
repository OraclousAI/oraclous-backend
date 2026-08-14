"""Shared citation contract — the resolvable source identity of a knowledge record (§CITE rev3).

The rule the whole Contract exists to enforce: **the platform mints every citation in code; a model
can reference one, it can never author one.**

The public surface is deliberately small — the models, the deterministic identity, the shared
minting function, and the graph-store encoding pair. The strip predicate that write and read paths
share lives in :mod:`oraclous_citation.graph_properties`, since it is service plumbing rather than
part of the Contract shape.

Canonical home for the shape itself: ``oraclous-knowledge`` ``flows/interface-contracts.md`` §CITE.
The JSON Schema and samples under ``contract/`` are the same shape as bytes; the frontend copies
them verbatim.
"""

from oraclous_citation.graph_properties import citation_from_properties, citation_properties
from oraclous_citation.identity import compute_citation_id
from oraclous_citation.minting import mint_citation
from oraclous_citation.models import UPLOAD_SOURCE_SYSTEM, Author, Citation, SourceRef

__all__ = [
    "UPLOAD_SOURCE_SYSTEM",
    "Author",
    "Citation",
    "SourceRef",
    "citation_from_properties",
    "citation_properties",
    "compute_citation_id",
    "mint_citation",
]
