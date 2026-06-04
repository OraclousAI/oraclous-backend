"""Recipe library repository (ORAA-4 §21 repositories layer — the only `recipes` SQL).

Org-scoped (ADR-006). Versioned: `store` inserts a NEW (id, version, org) row — never an UPDATE.
`get_latest` returns the highest-version recipe_json for an id; `list_summaries` lists the latest
version per id. Validation happens in the service before store.
"""

from __future__ import annotations

import uuid

from oraclous_substrate.access import enforced_organisation_id
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oraclous_knowledge_graph_service.repositories.models import Recipe


class RecipeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _org(self) -> uuid.UUID:
        return uuid.UUID(enforced_organisation_id())

    async def store(self, recipe_json: dict) -> dict:
        org = self._org()
        recipe_id = recipe_json["id"]
        max_version = await self._session.scalar(
            select(func.max(Recipe.version)).where(
                Recipe.id == recipe_id, Recipe.organisation_id == org
            )
        )
        next_version = 1 if max_version is None else int(max_version) + 1
        doc = dict(recipe_json)
        doc["version"] = next_version
        doc["status"] = "promoted"
        applies = recipe_json.get("applies_to", {})
        row = Recipe(
            id=recipe_id,
            version=next_version,
            organisation_id=org,
            status="promoted",
            source_type=applies.get("source_type", ""),
            shape_signature=applies.get("shape_signature", ""),
            concern=recipe_json.get("concern", ""),
            recipe_json=doc,
            authored_by=recipe_json.get("authoring", {}).get("authored_by"),
        )
        self._session.add(row)
        await self._session.flush()
        return {"id": recipe_id, "version": next_version, "status": "promoted"}

    async def get_latest(self, recipe_id: str) -> dict | None:
        row = await self._session.scalar(
            select(Recipe)
            .where(Recipe.id == recipe_id, Recipe.organisation_id == self._org())
            .order_by(Recipe.version.desc())
            .limit(1)
        )
        return dict(row.recipe_json) if row is not None else None

    async def list_summaries(self) -> list[dict]:
        rows = (
            (
                await self._session.execute(
                    select(Recipe)
                    .where(Recipe.organisation_id == self._org())
                    .order_by(Recipe.id, Recipe.version.desc())
                )
            )
            .scalars()
            .all()
        )
        seen: set[str] = set()
        out: list[dict] = []
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            out.append(
                {
                    "id": row.id,
                    "version": row.version,
                    "status": row.status,
                    "source_type": row.source_type,
                    "concern": row.concern,
                }
            )
        return out
