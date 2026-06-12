"""Route table (ORAA-4 §21 domain layer) — pure, no I/O.

Maps a request path-prefix to the upstream base URL that serves it, resolved by **longest-prefix
match** so colliding stems disambiguate correctly (e.g. ``/api/v1/graphs`` → knowledge-graph vs
``/api/v1/capabilities`` → capability-registry, both under ``/api/v1``). The platform-internal
``/internal/*`` plane is deliberately absent — it is never edge-routed.
"""

from __future__ import annotations

from dataclasses import dataclass

# Static prefix → upstream-settings-attribute map (the routing contract). The upstream base URLs are
# resolved from Settings at build time, so retargeting an upstream is config, not code.
_ROUTES: tuple[tuple[str, str], ...] = (
    # auth-service (identity + orgs + invitations + oauth)
    ("/v1/auth", "AUTH_SERVICE_URL"),
    ("/v1/orgs", "AUTH_SERVICE_URL"),
    ("/v1/invitations", "AUTH_SERVICE_URL"),
    ("/oauth", "AUTH_SERVICE_URL"),
    # credential-broker
    ("/credentials", "CREDENTIAL_BROKER_URL"),
    # knowledge-graph (graphs/recipes; /api/v1/graphs/{id}/ontology and /{id}/resolution/{cid}/...
    # — the HITL SAME_AS_CANDIDATE approve/reject mutation, #279 — both live under /api/v1/graphs)
    ("/api/v1/graphs", "KNOWLEDGE_GRAPH_URL"),
    ("/api/v1/recipes", "KNOWLEDGE_GRAPH_URL"),
    # knowledge-retriever
    ("/v1/search", "KNOWLEDGE_RETRIEVER_URL"),
    ("/v1/graph", "KNOWLEDGE_RETRIEVER_URL"),
    # capability-registry
    ("/api/v1/capabilities", "CAPABILITY_REGISTRY_URL"),
    ("/api/v1/tools", "CAPABILITY_REGISTRY_URL"),
    ("/api/v1/instances", "CAPABILITY_REGISTRY_URL"),
    ("/api/v1/executions", "CAPABILITY_REGISTRY_URL"),
    # harness-runtime (authenticated; never on the public allow-list)
    ("/v1/harnesses", "HARNESS_RUNTIME_URL"),
    # execution-engine (authenticated; never on the public allow-list)
    ("/v1/engine", "EXECUTION_ENGINE_URL"),
)


@dataclass(frozen=True)
class RouteEntry:
    prefix: str
    upstream_url: str  # upstream base URL (no trailing slash)


class RouteTable:
    """Longest-prefix-match resolver from request path → upstream base URL."""

    def __init__(self, entries: list[RouteEntry]) -> None:
        # longest prefix first so the most specific route wins on shared stems
        self._entries = sorted(entries, key=lambda e: len(e.prefix), reverse=True)

    @property
    def entries(self) -> list[RouteEntry]:
        return list(self._entries)

    def resolve(self, path: str) -> RouteEntry | None:
        """Return the upstream for ``path``, or None if no prefix matches (→ caller 404s)."""
        for e in self._entries:
            if path == e.prefix or path.startswith(e.prefix + "/"):
                return e
        return None


def build_route_table(settings) -> RouteTable:  # noqa: ANN001 — Settings (avoid core import in domain)
    """Build the route table from the upstream base URLs in Settings."""
    entries = [
        RouteEntry(prefix=prefix, upstream_url=getattr(settings, attr).rstrip("/"))
        for prefix, attr in _ROUTES
    ]
    return RouteTable(entries)
