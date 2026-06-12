"""Unit: the pure per-key CORS policy — path scoping, fail-closed origin check, header rewrite."""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.domain.cors_policy import (
    is_public_agent_path,
    is_public_plane_method,
    is_public_plane_preflight,
    origin_allowed,
    preflight_headers,
    rewrite_response_headers,
)

pytestmark = pytest.mark.unit


def test_path_scoping_is_only_the_slug_routes() -> None:
    assert is_public_agent_path("/v1/agents/weather")
    assert is_public_agent_path("/v1/agents/weather/invoke")
    # the member plane (bare /v1/agents publish/list) + everything else is OUT of scope
    assert not is_public_agent_path("/v1/agents")
    assert not is_public_agent_path("/v1/integration-keys")
    assert not is_public_agent_path("/health")


def test_public_plane_preflight_owns_get_post_and_bare_defers_delete() -> None:
    # GET (metadata) + POST (invoke) are the public plane -> AgentCors owns them
    assert is_public_plane_preflight(b"GET")
    assert is_public_plane_preflight(b"POST")
    assert is_public_plane_preflight(b"post")  # case-insensitive / defensive
    assert is_public_plane_preflight(None)  # a bare metadata-read preflight keeps today's behaviour
    # the member plane (DELETE unpublish, #289) + any other method are NOT public plane -> defer
    assert not is_public_plane_preflight(b"DELETE")
    assert not is_public_plane_preflight(b"PUT")
    assert not is_public_plane_preflight(b"PATCH")


def test_public_plane_method_owns_get_post_defers_delete() -> None:
    # the ACTUAL-request gate (#289): GET (metadata) / POST (invoke) are public-plane, so AgentCors
    # rewrites the response ACAO to the per-key decision; DELETE (member unpublish) + any other
    # method defer, so the gateway-wide CORS's ACAO survives on the response. `scope["method"]` is a
    # str (not bytes), and an actual request always has a method, so None -> defer (fail-safe).
    assert is_public_plane_method("GET")
    assert is_public_plane_method("POST")
    assert is_public_plane_method("get")  # case-insensitive / defensive
    assert not is_public_plane_method(None)
    assert not is_public_plane_method("DELETE")
    assert not is_public_plane_method("PUT")
    assert not is_public_plane_method("PATCH")


def test_origin_allowed_is_fail_closed_on_none() -> None:
    assert origin_allowed("https://a.example", ["https://a.example"])
    assert not origin_allowed("https://a.example", ["https://b.example"])
    assert not origin_allowed("https://a.example", [])  # empty list allows nothing
    assert not origin_allowed("https://a.example", None)  # no per-key policy -> deny


def test_preflight_reflects_origin_without_credentials() -> None:
    hdrs = dict(preflight_headers(b"https://a.example", b"authorization, x-foo"))
    assert hdrs[b"access-control-allow-origin"] == b"https://a.example"
    assert hdrs[b"access-control-allow-methods"] == b"GET, POST, OPTIONS"
    assert hdrs[b"access-control-allow-headers"] == b"authorization, x-foo"  # echoes requested
    assert b"access-control-allow-credentials" not in hdrs  # NEVER credentials on this plane
    assert hdrs[b"vary"] == b"Origin"


def test_rewrite_strips_inner_cors_then_sets_per_key_acao() -> None:
    # the inner gateway-wide CORS echoed the origin WITH credentials — both must be replaced
    inner = [
        (b"content-type", b"application/json"),
        (b"access-control-allow-origin", b"https://a.example"),
        (b"access-control-allow-credentials", b"true"),
        (b"vary", b"Origin"),
    ]
    # allowed origin -> exactly one ACAO, no credentials, Vary:Origin kept
    out = rewrite_response_headers(inner, b"https://a.example", ["https://a.example"])
    acao = [v for k, v in out if k == b"access-control-allow-origin"]
    assert acao == [b"https://a.example"]
    assert not any(k == b"access-control-allow-credentials" for k, _ in out)
    assert (b"vary", b"Origin") in out
    assert (b"content-type", b"application/json") in out  # untouched


def test_mint_request_validates_cors_origins() -> None:
    from oraclous_application_gateway_service.schema.integration_key_schemas import MintKeyRequest
    from pydantic import ValidationError

    # accepts real origins (incl. localhost:port); rejects trailing slash/path, wildcard, newline
    MintKeyRequest(
        bound_agent_slug="a", cors_origins=["http://localhost:3000", "https://x.example"]
    )
    for bad in ["https://x.com/", "https://x.com/path", "*", "https://x.com\n", "ftp://x.com"]:
        with pytest.raises(ValidationError):
            MintKeyRequest(bound_agent_slug="a", cors_origins=[bad])


def test_rewrite_denies_unlisted_origin_and_none_policy() -> None:
    inner = [(b"access-control-allow-origin", b"https://evil.example")]
    # unlisted origin -> NO ACAO (the inner one is stripped, none added)
    out = rewrite_response_headers(inner, b"https://evil.example", ["https://good.example"])
    assert not any(k == b"access-control-allow-origin" for k, _ in out)
    # a key with no cors_origins -> NO ACAO either (fail-closed)
    out2 = rewrite_response_headers(inner, b"https://evil.example", None)
    assert not any(k == b"access-control-allow-origin" for k, _ in out2)
