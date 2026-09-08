"""#949 ruling Q4 (C5, KRS half) — with `KRS_EMBEDDER=openai` and no resolvable organisation
credential, meaning-based search REFUSES with a typed, actionable 422. It never silently degrades
to word-overlap (the exact failure #949 exists to close), and it never returns an empty result set
with no explanation (the console must render a configuration prompt, not a blank screen).

Mechanism mirrors the existing `get_org_judge` -> `_JUDGE_UNCONFIGURED` typed-422 DI pattern
(`core/dependencies.py`), but the body carries a top-level `error_code` inside the FastAPI
`detail` wrapper — the shape the gateway's `extract_error_code` (#866) already understands — so
the specific, curated message survives the gateway hop instead of collapsing into the generic
VALIDATION_FAILED "one or more fields are invalid" envelope (C5's other half, gateway-side, is
pinned in `services/application-gateway-service/tests/`).

`get_retrieval_service` does not yet resolve an embedder or raise this refusal — every not-yet-built
seam use is FUNCTION-LOCAL (`.claude/rules/tests-seam-imports.md`); this file hard-fails RED until
the `[impl]` lands.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}
_BODY = {"query": "who wrote the first program?", "graph_id": str(uuid.uuid4())}


class _FakeDriver:
    """A Neo4j driver stand-in so `get_neo4j_driver` succeeds and execution reaches the
    embedder-resolution step this test actually exercises."""

    def execute_query(self, *_a, **_kw):
        return ([], None, None)


def _core_dependencies_module():
    import oraclous_knowledge_retriever_service.core.dependencies as deps  # noqa: PLC0415

    return deps


def _wire_openai_mode_that_refuses(deps, monkeypatch: pytest.MonkeyPatch, app) -> None:
    from oraclous_knowledge_retriever_service.services.broker_client import (  # noqa: PLC0415
        BrokerError,
    )

    async def _refuse(*_a, **_kw):
        raise BrokerError("model_credential_not_configured: nothing configured for this org")

    monkeypatch.setattr(deps, "get_settings", lambda: deps.Settings(embedder="openai"))
    monkeypatch.setattr(deps, "resolve_embedder_for_org", _refuse)
    app.dependency_overrides[deps.get_neo4j_driver] = lambda: _FakeDriver()


async def test_openai_mode_with_no_org_credential_is_a_typed_422(
    app, async_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps = _core_dependencies_module()
    _wire_openai_mode_that_refuses(deps, monkeypatch, app)
    try:
        resp = await async_client.post("/v1/search/semantic", json=_BODY, headers=_AUTH)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, dict), "must be the dict-detail shape extract_error_code understands"
    assert detail["error_code"] == "MODEL_CREDENTIAL_REQUIRED"
    assert "model credential" in detail["msg"].lower()
    assert "search" in detail["msg"].lower()


async def test_the_refusal_never_silently_falls_back_to_word_overlap(
    app, async_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #949 failure mode is not "returns an error" — it is "returns SOMETHING that looks like
    an answer". This asserts the response is the refusal envelope, not a 200 with results."""
    deps = _core_dependencies_module()
    _wire_openai_mode_that_refuses(deps, monkeypatch, app)
    try:
        resp = await async_client.post("/v1/search/semantic", json=_BODY, headers=_AUTH)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code != 200


async def test_hybrid_search_also_refuses_because_it_needs_the_semantic_leg(
    app, async_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps = _core_dependencies_module()
    _wire_openai_mode_that_refuses(deps, monkeypatch, app)
    try:
        resp = await async_client.post("/v1/search/hybrid", json=_BODY, headers=_AUTH)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error_code"] == "MODEL_CREDENTIAL_REQUIRED"


async def test_hashing_mode_is_unaffected_by_this_refusal_path(
    app, async_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit offline/CI selection (#949's carve-out) must never trip this refusal — a
    regression here would break every key-free dev/CI run."""
    deps = _core_dependencies_module()

    def _boom(*_a, **_kw):
        raise AssertionError("hashing mode must never call the credential resolver")

    monkeypatch.setattr(deps, "get_settings", lambda: deps.Settings(embedder="hashing"))
    monkeypatch.setattr(deps, "resolve_embedder_for_org", _boom)
    app.dependency_overrides[deps.get_neo4j_driver] = lambda: _FakeDriver()

    try:
        resp = await async_client.post("/v1/search/semantic", json=_BODY, headers=_AUTH)
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert resp.json() == []
