"""Recipe library routes (ORAA-4 §21 routes layer) — store/list/get recipes (org-scoped).

Slice C adds two authoring aids alongside the library CRUD:
  POST /recipes/dry-run   — preview a recipe over a sample with NO Neo4j writes (node labels +
                            counts + relationship types + ontology violations).
  GET  /recipes/templates — the built-in, author-ready recipe templates (the evidence/conflicts
                            pair from Slice A's ``services/recipes/templates.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from oraclous_knowledge_graph_service.core.dependencies import (
    DryRunServiceDep,
    RecipeServiceDep,
    UserIdDep,
)
from oraclous_knowledge_graph_service.schema.recipe_schemas import (
    DryRunRequest,
    RecipeStoredResponse,
    RecipeSummary,
    RecipeTemplate,
    StoreRecipeRequest,
)
from oraclous_knowledge_graph_service.services.dry_run_service import DryRunError
from oraclous_knowledge_graph_service.services.recipe_service import RecipeValidationError
from oraclous_knowledge_graph_service.services.recipes.templates import (
    EVIDENCE_CONCERN,
    build_conflicts_recipe,
    build_evidence_recipe,
)

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


@router.get("/templates", response_model=list[RecipeTemplate])
async def list_recipe_templates(_user_id: UserIdDep) -> list[RecipeTemplate]:
    """The built-in, author-ready recipe templates — the evidence-and-conflicts pair (Slice A)."""
    return [
        RecipeTemplate(concern=EVIDENCE_CONCERN, recipe=build_evidence_recipe()),
        RecipeTemplate(concern=EVIDENCE_CONCERN, recipe=build_conflicts_recipe()),
    ]


@router.post("/dry-run")
async def dry_run_recipe(
    body: DryRunRequest, service: DryRunServiceDep, _user_id: UserIdDep
) -> dict:
    """Preview a recipe projection over a sample with NO Neo4j writes (authoring aid)."""
    try:
        return service.preview(
            sample=body.sample,
            source_type=body.source_type,
            recipe=body.recipe,
            ontology=body.ontology,
        )
    except DryRunError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except RecipeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.post("/{recipe_id}/promote", response_model=RecipeStoredResponse)
async def promote_recipe(
    recipe_id: str, service: RecipeServiceDep, _user_id: UserIdDep
) -> RecipeStoredResponse:
    """Promote a draft recipe to the runnable 'promoted' state (in place; no version bump).
    Idempotent. 404 if the recipe is unknown."""
    promoted = await service.promote(recipe_id)
    if promoted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="recipe not found")
    return RecipeStoredResponse(**promoted)


@router.get("/{recipe_id}")
async def get_recipe(recipe_id: str, service: RecipeServiceDep, _user_id: UserIdDep) -> dict:
    recipe = await service.get(recipe_id)
    if recipe is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="recipe not found")
    return recipe
