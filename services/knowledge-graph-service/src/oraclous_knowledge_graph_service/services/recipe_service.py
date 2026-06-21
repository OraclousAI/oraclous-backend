"""Recipe library use-cases (services layer).

Validate-then-store: a submitted recipe is validated against the format-0.2 schema + the safe-
identifier rules (the same engine validation used at run time) before it is persisted, so a bad
recipe is rejected at POST time rather than mid-ingest. Org scope is enforced in the repo.
"""

from __future__ import annotations

from oraclous_knowledge_graph_service.repositories.recipe_repository import RecipeRepository
from oraclous_knowledge_graph_service.services.recipes.engine import (
    RecipeExecutionEngine,
    RecipeValidationError,
)


class RecipeService:
    def __init__(self, repo: RecipeRepository, engine: RecipeExecutionEngine) -> None:
        self._repo = repo
        self._engine = engine

    async def store(self, recipe_json: dict) -> dict:
        # validate a version-1 view (store bumps version) so schema's version>=1 holds
        candidate = dict(recipe_json)
        candidate.setdefault("version", 1)
        candidate.setdefault("status", "draft")
        self._engine.validate(candidate)  # raises RecipeValidationError on bad input
        return await self._repo.store(recipe_json)

    async def promote(self, recipe_id: str) -> dict | None:
        """Promote a draft recipe to promoted (the runnable state). Returns ``{id, version,
        status}`` or ``None`` if the recipe is unknown (the route maps that to 404). Idempotent."""
        return await self._repo.promote(recipe_id)

    async def get(self, recipe_id: str) -> dict | None:
        return await self._repo.get_latest(recipe_id)

    async def list(self) -> list[dict]:
        return await self._repo.list_summaries()


__all__ = ["RecipeService", "RecipeValidationError"]
