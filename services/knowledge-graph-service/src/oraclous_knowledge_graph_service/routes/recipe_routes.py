"""Recipe library routes (ORAA-4 §21 routes layer) — store/list/get recipes (org-scoped)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_graph_service.core.dependencies import RecipeServiceDep, UserIdDep
from oraclous_knowledge_graph_service.schema.recipe_schemas import (
    RecipeStoredResponse,
    RecipeSummary,
    StoreRecipeRequest,
)
from oraclous_knowledge_graph_service.services.recipe_service import RecipeValidationError

router = APIRouter(prefix="/api/v1/recipes", tags=["recipes"])


@router.post("", response_model=RecipeStoredResponse, status_code=status.HTTP_201_CREATED)
async def store_recipe(
    body: StoreRecipeRequest, service: RecipeServiceDep, _user_id: UserIdDep
) -> RecipeStoredResponse:
    try:
        stored = await service.store(body.recipe)
    except RecipeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return RecipeStoredResponse(**stored)


@router.get("", response_model=list[RecipeSummary])
async def list_recipes(service: RecipeServiceDep, _user_id: UserIdDep) -> list[RecipeSummary]:
    return [RecipeSummary(**r) for r in await service.list()]


@router.get("/{recipe_id}")
async def get_recipe(recipe_id: str, service: RecipeServiceDep, _user_id: UserIdDep) -> dict:
    recipe = await service.get(recipe_id)
    if recipe is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="recipe not found")
    return recipe
