"""Recipe library HTTP layer (R3.5-P1-S3) — store/get/list with the real RecipeService + engine
validation, backed by an in-memory fake repo. Auth (401)/validation (422)/not-found (404) are real.
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.core.dependencies import get_recipe_service
from oraclous_knowledge_graph_service.services.recipe_service import RecipeService
from oraclous_knowledge_graph_service.services.recipes.engine import get_recipe_engine

pytestmark = pytest.mark.integration

_AUTH = {"Authorization": "Bearer dev-token"}

_VALID = {
    "recipe_format_version": "0.2",
    "id": "rcp_test",
    "version": 1,
    "status": "draft",
    "concern": "test",
    "applies_to": {"source_type": "csv", "shape_signature": "csv(a:str)"},
    "mappings": [
        {
            "id": "r",
            "project_to": "node",
            "label": "Thing",
            "match": {"unit_kind": "record"},
            "identity": {"scheme": "deterministic", "from": ["column:a"]},
        }
    ],
}


class _FakeRecipeRepo:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict]] = {}

    async def store(self, recipe_json: dict) -> dict:
        rid = recipe_json["id"]
        versions = self.rows.setdefault(rid, [])
        version = len(versions) + 1
        doc = dict(recipe_json)
        doc["version"] = version
        doc["status"] = "draft"  # store saves a DRAFT — promote makes it runnable (ADR-028)
        versions.append(doc)
        return {"id": rid, "version": version, "status": "draft"}

    async def promote(self, recipe_id: str) -> dict | None:
        versions = self.rows.get(recipe_id)
        if not versions:
            return None
        latest = versions[-1]
        latest["status"] = "promoted"  # in place, no new version
        return {"id": recipe_id, "version": latest["version"], "status": "promoted"}

    async def get_latest(self, recipe_id: str) -> dict | None:
        versions = self.rows.get(recipe_id)
        return dict(versions[-1]) if versions else None

    async def list_summaries(self) -> list[dict]:
        return [
            {
                "id": rid,
                "version": len(v),
                "status": v[-1].get("status", "draft"),
                "source_type": v[-1].get("applies_to", {}).get("source_type", ""),
                "concern": v[-1].get("concern", ""),
            }
            for rid, v in self.rows.items()
        ]


@pytest.fixture
def recipe_service() -> RecipeService:
    return RecipeService(_FakeRecipeRepo(), get_recipe_engine())


@pytest.fixture
def client(app, async_client, recipe_service):
    app.dependency_overrides[get_recipe_service] = lambda: recipe_service
    yield async_client
    app.dependency_overrides.clear()


async def test_recipes_require_auth(client) -> None:
    # UserIdDep is real even with the service overridden -> 401 fires without any DB access
    assert (await client.get("/api/v1/recipes")).status_code == 401


async def test_store_saves_a_draft(client) -> None:
    stored = await client.post("/api/v1/recipes", json={"recipe": _VALID}, headers=_AUTH)
    assert stored.status_code == 201, stored.text
    assert stored.json()["id"] == "rcp_test"
    # store saves a DRAFT (not auto-promoted) — it is not runnable until promoted (ADR-028)
    assert stored.json()["status"] == "draft"
    got = await client.get("/api/v1/recipes/rcp_test", headers=_AUTH)
    assert got.status_code == 200 and got.json()["id"] == "rcp_test"
    listed = await client.get("/api/v1/recipes", headers=_AUTH)
    assert listed.status_code == 200 and len(listed.json()) == 1
    assert listed.json()[0]["status"] == "draft"


async def test_promote_flips_draft_to_promoted(client) -> None:
    await client.post("/api/v1/recipes", json={"recipe": _VALID}, headers=_AUTH)
    promoted = await client.post("/api/v1/recipes/rcp_test/promote", headers=_AUTH)
    assert promoted.status_code == 200, promoted.text
    # promote keeps the same version (in place) and flips the status to the runnable state
    assert promoted.json() == {"id": "rcp_test", "version": 1, "status": "promoted"}
    # the change is visible to readers (the list now shows promoted)
    listed = await client.get("/api/v1/recipes", headers=_AUTH)
    assert listed.json()[0]["status"] == "promoted"


async def test_promote_unknown_recipe_is_404(client) -> None:
    assert (await client.post("/api/v1/recipes/rcp_nope/promote", headers=_AUTH)).status_code == 404


async def test_store_invalid_recipe_is_422(client) -> None:
    bad = {"recipe_format_version": "0.2", "id": "bad"}  # missing required + bad id pattern
    resp = await client.post("/api/v1/recipes", json={"recipe": bad}, headers=_AUTH)
    assert resp.status_code == 422


async def test_get_unknown_recipe_is_404(client) -> None:
    assert (await client.get("/api/v1/recipes/rcp_nope", headers=_AUTH)).status_code == 404
