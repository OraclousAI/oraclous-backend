"""Per-key CORS policy (ORAA-4 §21 domain layer) — pure origin + header logic for the agent plane.

The published-agent widget routes (``GET /v1/agents/{slug}`` + ``.../invoke``) are the only
browser-embeddable surface. Each integration key carries its own ``cors_origins`` allow-list. A
browser **preflight** (``OPTIONS``) carries the Origin but NO ``Authorization`` — the key is unknown
— so it is answered **permissively** (reflect the origin, NO credentials): execution is gated by the
key, response-read by the actual keyed request's per-key ``Access-Control-Allow-Origin``. On the
keyed response the ACAO is set only when the request Origin is in the presenting key's
``cors_origins`` (else no ACAO — the browser cannot read the response). No credentials are ever
allowed on this plane (the key is an explicit ``Authorization`` header, not a cookie), so it never
falls into the wildcard-origin-with-credentials trap.
"""

from __future__ import annotations

_PUBLIC_AGENT_PREFIX = "/v1/agents/"

# the methods the PUBLIC plane owns on /v1/agents/{slug}[/invoke]: GET (metadata) + POST (invoke).
# A preflight requesting any OTHER method (e.g. DELETE = the member-plane admin unpublish, #289) is
# NOT public-plane — AgentCorsMiddleware must defer it to the gateway-wide CORS rather than answer
# with the per-key public-plane policy (which never advertises DELETE nor the console origin).
_PUBLIC_PLANE_METHODS = frozenset({b"GET", b"POST"})

# the CORS response headers we own on the agent plane: stripped from whatever the inner gateway-wide
# CORS emitted, then re-set from the per-key decision (replace, never append → exactly one ACAO).
_MANAGED = frozenset(
    {
        b"access-control-allow-origin",
        b"access-control-allow-credentials",
        b"access-control-expose-headers",  # defensive — strip if the inner CORS adds it later
        b"vary",
    }
)


def is_public_agent_path(path: str) -> bool:
    """The two browser-embeddable routes: ``/v1/agents/{slug}`` and ``/v1/agents/{slug}/invoke``.
    Excludes the bare member routes (``/v1/agents`` exact — publish/list, member JWT)."""
    return path.startswith(_PUBLIC_AGENT_PREFIX) and path != _PUBLIC_AGENT_PREFIX.rstrip("/")


def is_public_plane_preflight(request_method: bytes | None) -> bool:
    """Does this OPTIONS preflight belong to the public plane (so AgentCors owns it)?

    True for the public-plane methods (GET metadata, POST invoke) AND for an absent
    ``Access-Control-Request-Method`` (a bare metadata-read preflight — keep today's behaviour).
    False for the member plane (e.g. DELETE unpublish, #289) — those defer to the gateway-wide CORS,
    which advertises DELETE + the console origin. Compared case-insensitively (browsers send the
    method verbatim, but be defensive)."""
    if request_method is None:
        return True
    return request_method.strip().upper() in _PUBLIC_PLANE_METHODS


def origin_allowed(origin: str, cors_origins: list[str] | None) -> bool:
    """Fail-closed: a key with no ``cors_origins`` (None) allows NO browser origin."""
    return cors_origins is not None and origin in cors_origins


def preflight_headers(origin: bytes, request_headers: bytes | None) -> list[tuple[bytes, bytes]]:
    """Answer a published-agent preflight permissively (no credentials): reflect the origin + the
    method/headers the browser asked for. Safe because a preflight only lets the browser SEND the
    keyed request — the key gates execution and the actual response's ACAO gates the read."""
    allow_headers = request_headers if request_headers else b"authorization, content-type"
    return [
        (b"access-control-allow-origin", origin),
        (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
        (b"access-control-allow-headers", allow_headers),
        (b"access-control-max-age", b"600"),
        (b"vary", b"Origin"),
    ]


def rewrite_response_headers(
    raw: list[tuple[bytes, bytes]], origin: bytes, cors_origins: list[str] | None
) -> list[tuple[bytes, bytes]]:
    """Strip the inner gateway-wide CORS headers we manage, always re-assert ``Vary: Origin`` (the
    response varies by origin on this plane), and add the per-key ``Access-Control-Allow-Origin``
    IFF the origin is allowed. An unlisted origin / a key with no ``cors_origins`` gets NO ACAO."""
    out = [(k, v) for k, v in raw if k.lower() not in _MANAGED]
    out.append((b"vary", b"Origin"))
    if origin_allowed(origin.decode("latin-1"), cors_origins):
        out.append((b"access-control-allow-origin", origin))
    return out
