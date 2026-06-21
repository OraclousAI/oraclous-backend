"""Upstream targets (domain layer) — pure, no I/O.

The named set of upstream services the gateway health-aggregates (base URLs come from Settings).
"""

from __future__ import annotations


def upstream_health_targets(settings) -> dict[str, str]:  # noqa: ANN001 — Settings, avoid core import
    """Name → upstream base URL (no trailing slash) for every fronted service."""
    return {
        "auth": settings.AUTH_SERVICE_URL.rstrip("/"),
        "credential-broker": settings.CREDENTIAL_BROKER_URL.rstrip("/"),
        "knowledge-graph": settings.KNOWLEDGE_GRAPH_URL.rstrip("/"),
        "knowledge-retriever": settings.KNOWLEDGE_RETRIEVER_URL.rstrip("/"),
        "capability-registry": settings.CAPABILITY_REGISTRY_URL.rstrip("/"),
        "harness-runtime": settings.HARNESS_RUNTIME_URL.rstrip("/"),
        "execution-engine": settings.EXECUTION_ENGINE_URL.rstrip("/"),
    }
