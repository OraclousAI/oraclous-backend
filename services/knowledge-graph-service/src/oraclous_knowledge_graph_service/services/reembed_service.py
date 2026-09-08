"""Re-embed service helpers (services layer) — #949 Q2.

Just the advisory lock key, kept out of the tasks module for the same reason
`memory_consolidation_lock_key` is: computing it must not require importing Celery, so a status or
admin surface can name the same lock the worker holds without dragging in a broker connection.
"""

from __future__ import annotations


def reembed_lock_key(*, organisation_id: str, graph_id: str) -> str:
    """The per-(org,graph) advisory lock key the re-embed pass holds (#303/#305 pattern).

    Per graph, not per organisation: two graphs of one organisation are independent workspaces and
    serialising them would make a large tenant's migration take as long as the sum of its parts.
    """
    return f"kgs:chunks:reembed:{organisation_id}:{graph_id}"
