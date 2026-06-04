"""Public contracts for the knowledge-graph service write path.

NodeResult is the canonical envelope returned by ingest and upload
endpoints: a minimal OHM-shaped dict carrying node identity (id, type)
and an open-ended properties bag.
"""

from __future__ import annotations

from typing import Any, TypedDict


class NodeResult(TypedDict):
    """OHM envelope for a persisted knowledge-graph node.

    Returned by ingest and upload write-path endpoints.  The properties
    dict carries both domain properties and write-path provenance stamps
    (graph_id, transaction_time, ingestion_time, ingestion_source).
    """

    id: str
    type: str
    properties: dict[str, Any]
