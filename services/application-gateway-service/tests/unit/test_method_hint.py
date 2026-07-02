"""Unit: the curated wrong-method hint map (#579 Decision 3) — a pure suggestion, never a relay."""

from __future__ import annotations

import pytest
from oraclous_application_gateway_service.domain.method_hint import suggest_method_hint

pytestmark = pytest.mark.unit


def test_post_documents_returns_the_ingest_hint() -> None:
    hint = suggest_method_hint("POST", "/api/v1/graphs/2b1c/documents")
    assert hint is not None
    # names the right verbs/routes so a POST-to-add-content guess self-corrects in one step.
    assert "upload" in hint and "/ingest" in hint and "read-only" in hint


def test_match_is_case_insensitive_and_tolerates_a_trailing_slash() -> None:
    assert suggest_method_hint("post", "/api/v1/graphs/g/documents/") is not None


def test_non_matching_method_or_path_returns_none() -> None:
    assert suggest_method_hint("GET", "/api/v1/graphs/g/documents") is None  # GET /docs is fine
    assert suggest_method_hint("POST", "/api/v1/graphs/g/upload") is None  # a real POST target
    assert suggest_method_hint("DELETE", "/api/v1/graphs/g/documents") is None
    assert suggest_method_hint("POST", "/v1/search") is None  # an unrelated resource


def test_the_hint_is_a_constant_and_never_echoes_the_request_path() -> None:
    # §3 rule 8: the returned string must NOT reflect the tenant id / the input path.
    tenant = "secret-tenant-0xdeadbeef"
    hint = suggest_method_hint("POST", f"/api/v1/graphs/{tenant}/documents")
    assert hint is not None and tenant not in hint
    assert hint == suggest_method_hint("POST", "/api/v1/graphs/other/documents")  # id-independent
