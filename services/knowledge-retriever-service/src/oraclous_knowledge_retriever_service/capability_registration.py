"""Capability registration for knowledge-retriever-service.

Registers all five KRS retrieval endpoints as OHM kind:tool descriptors in the
capability registry.  One module-level list (`RETRIEVER_CAPABILITY_DESCRIPTORS`)
holds the static descriptor dicts; `register_retriever_capabilities` persists
them for a given org via `CapabilityRegistryService.create`.

Story: ORAA-62 [R3-CAP-1]
"""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models.capability_descriptor import CapabilityDescriptorDB
    from app.services.capability_registry import CapabilityRegistryService


def _version_hash(identifier: str) -> str:
    """Deterministic SHA-256 placeholder from the descriptor's canonical identifier."""
    return hashlib.sha256(f"{identifier}:1.0.0".encode()).hexdigest()


RETRIEVER_CAPABILITY_DESCRIPTORS: list[dict[str, Any]] = [
    {
        "kind": "tool",
        "id": "semantic-search",
        "version": {
            "hash": _version_hash("semantic-search"),
            "tags": ["1.0.0"],
        },
        "metadata": {
            "name": "Semantic Search",
            "description": (
                "Retrieve documents ranked by embedding-based semantic similarity "
                "to the query text."
            ),
        },
        "spec": {
            "implementation": {
                "type": "http",
                "handler": "/v1/search/semantic",
            },
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language query to embed and search against.",
                    },
                    "org_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Organisation scope for the search.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum number of results to return.",
                    },
                },
                "required": ["query", "org_id"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "score": {"type": "number"},
                                "content": {"type": "string"},
                                "metadata": {"type": "object"},
                            },
                            "required": ["id", "score"],
                        },
                    },
                },
                "required": ["results"],
            },
            "credential_requirements": [],
        },
    },
    {
        "kind": "tool",
        "id": "full-text-search",
        "version": {
            "hash": _version_hash("full-text-search"),
            "tags": ["1.0.0"],
        },
        "metadata": {
            "name": "Full-Text Search",
            "description": (
                "Retrieve documents matching the query using full-text (BM25) search "
                "over the organisation's knowledge index."
            ),
        },
        "spec": {
            "implementation": {
                "type": "http",
                "handler": "/v1/search/fulltext",
            },
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Full-text query string.",
                    },
                    "org_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Organisation scope for the search.",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum number of results to return.",
                    },
                },
                "required": ["query", "org_id"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "score": {"type": "number"},
                                "content": {"type": "string"},
                                "metadata": {"type": "object"},
                            },
                            "required": ["id", "score"],
                        },
                    },
                },
                "required": ["results"],
            },
            "credential_requirements": [],
        },
    },
    {
        "kind": "tool",
        "id": "hybrid-search",
        "version": {
            "hash": _version_hash("hybrid-search"),
            "tags": ["1.0.0"],
        },
        "metadata": {
            "name": "Hybrid Search",
            "description": (
                "Retrieve documents using a weighted combination of semantic and "
                "full-text search scores."
            ),
        },
        "spec": {
            "implementation": {
                "type": "http",
                "handler": "/v1/search/hybrid",
            },
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query text.",
                    },
                    "org_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Organisation scope for the search.",
                    },
                    "semantic_weight": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "default": 0.5,
                        "description": (
                            "Weight given to the semantic score (0 = full-text only, "
                            "1 = semantic only)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum number of results to return.",
                    },
                },
                "required": ["query", "org_id"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "results": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "score": {"type": "number"},
                                "semantic_score": {"type": "number"},
                                "fulltext_score": {"type": "number"},
                                "content": {"type": "string"},
                                "metadata": {"type": "object"},
                            },
                            "required": ["id", "score"],
                        },
                    },
                },
                "required": ["results"],
            },
            "credential_requirements": [],
        },
    },
    {
        "kind": "tool",
        "id": "graph-traverse",
        "version": {
            "hash": _version_hash("graph-traverse"),
            "tags": ["1.0.0"],
        },
        "metadata": {
            "name": "Graph Traverse",
            "description": (
                "Traverse the organisation's knowledge graph from a start node up to "
                "a specified depth, returning nodes and edges encountered."
            ),
        },
        "spec": {
            "implementation": {
                "type": "http",
                "handler": "/v1/graph/traverse",
            },
            "input_schema": {
                "type": "object",
                "properties": {
                    "start_node_id": {
                        "type": "string",
                        "description": "ID of the node to begin traversal from.",
                    },
                    "org_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Organisation scope for the traversal.",
                    },
                    "depth": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 2,
                        "description": "Maximum hop depth from the start node.",
                    },
                },
                "required": ["start_node_id", "org_id"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "nodes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "kind": {"type": "string"},
                                "properties": {"type": "object"},
                            },
                            "required": ["id", "kind"],
                        },
                    },
                    "edges": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {"type": "string"},
                                "target": {"type": "string"},
                                "relation": {"type": "string"},
                            },
                            "required": ["source", "target", "relation"],
                        },
                    },
                },
                "required": ["nodes", "edges"],
            },
            "credential_requirements": [],
        },
    },
    {
        "kind": "tool",
        "id": "temporal-slice",
        "version": {
            "hash": _version_hash("temporal-slice"),
            "tags": ["1.0.0"],
        },
        "metadata": {
            "name": "Temporal Slice",
            "description": (
                "Retrieve a snapshot of the knowledge graph as it existed within a "
                "specified time window."
            ),
        },
        "spec": {
            "implementation": {
                "type": "http",
                "handler": "/v1/graph/temporal",
            },
            "input_schema": {
                "type": "object",
                "properties": {
                    "org_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Organisation scope for the temporal query.",
                    },
                    "start_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "Start of the time window (ISO 8601).",
                    },
                    "end_time": {
                        "type": "string",
                        "format": "date-time",
                        "description": "End of the time window (ISO 8601).",
                    },
                },
                "required": ["org_id", "start_time", "end_time"],
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "nodes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "kind": {"type": "string"},
                                "created_at": {"type": "string", "format": "date-time"},
                                "properties": {"type": "object"},
                            },
                            "required": ["id", "kind"],
                        },
                    },
                    "edges": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source": {"type": "string"},
                                "target": {"type": "string"},
                                "relation": {"type": "string"},
                                "created_at": {"type": "string", "format": "date-time"},
                            },
                            "required": ["source", "target", "relation"],
                        },
                    },
                    "snapshot_time": {
                        "type": "string",
                        "format": "date-time",
                    },
                },
                "required": ["nodes", "edges"],
            },
            "credential_requirements": [],
        },
    },
]


async def register_retriever_capabilities(
    svc: CapabilityRegistryService,
    org_id: uuid.UUID,
) -> list[CapabilityDescriptorDB]:
    """Register all five KRS retrieval capabilities for the given organisation.

    Calls `svc.create` for each entry in RETRIEVER_CAPABILITY_DESCRIPTORS and
    returns the persisted DB rows.  The repository auto-computes `content_hash`
    from the descriptor body on each call.
    """
    from app.models.capability_descriptor import DescriptorKind

    rows = []
    for descriptor in RETRIEVER_CAPABILITY_DESCRIPTORS:
        row = await svc.create(
            org_id=org_id,
            kind=DescriptorKind.TOOL,
            descriptor=descriptor,
        )
        rows.append(row)
    return rows
