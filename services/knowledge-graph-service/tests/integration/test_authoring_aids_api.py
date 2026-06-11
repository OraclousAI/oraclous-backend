"""Authoring-aids HTTP layer (Slice C) — templates, recipe dry-run, schema synthesis.

Real routes + dev-auth (401 paths are real). Dry-run uses the real DryRunService (no Neo4j — it
writes nothing). Schema synthesis's LLM is stubbed by monkeypatching the route's
``make_synthesizer`` so no network/key is needed; the 503 fail-closed path (no LLM) is also tested.
"""

from __future__ import annotations

import pytest
from neo4j_graphrag.experimental.components.schema import GraphSchema, NodeType, RelationshipType
from oraclous_knowledge_graph_service.routes import ontology_routes
from oraclous_knowledge_graph_service.services.schema_synthesis_service import (
    SchemaSynthesisService,
    SchemaSynthesisUnavailable,
)

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}


# --- templates ---------------------------------------------------------------
async def test_templates_requires_auth(async_client) -> None:
    resp = await async_client.get("/api/v1/recipes/templates")
    assert resp.status_code == 401


async def test_templates_returns_the_evidence_and_conflicts_pair(async_client) -> None:
    resp = await async_client.get("/api/v1/recipes/templates", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    templates = resp.json()
    assert len(templates) == 2
    assert all(t["concern"] == "evidence-and-conflicts" for t in templates)
    recipe_ids = {t["recipe"]["id"] for t in templates}
    assert recipe_ids == {"rcp_eurail-evidence", "rcp_eurail-conflicts"}


# --- dry-run -----------------------------------------------------------------
async def test_dry_run_requires_auth(async_client) -> None:
    resp = await async_client.post("/api/v1/recipes/dry-run", json={"sample": "[]"})
    assert resp.status_code == 401


async def test_dry_run_previews_a_structured_sample(async_client) -> None:
    body = {"sample": '[{"name": "Ada"}, {"name": "Grace"}]', "source_type": "json"}
    resp = await async_client.post("/api/v1/recipes/dry-run", json=body, headers=_AUTH)
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["node_labels"] == {"Record": 2}
    assert out["counts"]["nodes"] == 2


async def test_dry_run_empty_sample_is_422(async_client) -> None:
    body = {"sample": "[]", "source_type": "json"}
    resp = await async_client.post("/api/v1/recipes/dry-run", json=body, headers=_AUTH)
    assert resp.status_code == 422


# --- schema synthesis (POST /api/v1/ontology/suggest) ------------------------
async def test_suggest_requires_auth(async_client) -> None:
    resp = await async_client.post("/api/v1/ontology/suggest", json={"sample": "x"})
    assert resp.status_code == 401


async def test_suggest_returns_ontology_shaped_suggestion(async_client, monkeypatch) -> None:
    fixed = GraphSchema(
        node_types=(NodeType(label="Station"), NodeType(label="Operator")),
        relationship_types=(RelationshipType(label="OPERATED_BY", description=""),),
        patterns=(("Station", "OPERATED_BY", "Operator"),),
    )
    monkeypatch.setattr(
        ontology_routes, "make_synthesizer", lambda _s: SchemaSynthesisService(lambda _t: fixed)
    )
    resp = await async_client.post(
        "/api/v1/ontology/suggest",
        json={"sample": "Stations operated by operators", "mode": "strict"},
        headers=_AUTH,
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["mode"] == "strict"
    assert {e["name"] for e in out["entity_types"]} == {"Station", "Operator"}
    assert out["relationship_types"][0]["name"] == "OPERATED_BY"


async def test_suggest_is_503_when_no_llm_configured(async_client, monkeypatch) -> None:
    def _unavailable(_settings):
        raise SchemaSynthesisUnavailable("no LLM")

    monkeypatch.setattr(ontology_routes, "make_synthesizer", _unavailable)
    resp = await async_client.post("/api/v1/ontology/suggest", json={"sample": "x"}, headers=_AUTH)
    assert resp.status_code == 503
