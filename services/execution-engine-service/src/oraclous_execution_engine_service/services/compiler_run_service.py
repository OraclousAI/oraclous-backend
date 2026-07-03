"""Compiler-run service (services layer) — the Describe door's server side (#635, C-1(a)).

``POST /v1/engine/compiler-runs`` assembles the harness-compiler team server-side
(``build_compiler_team`` — ADR-047's compiler-as-a-team, previously constructible only by
``packages/ohm``), bakes the #596 seeded catalog into the surveyor's subgoal, binds the caller's
BYOM models into the manifest AND every sub-harness, then submits through the SAME
``TeamRunService.create`` path — a compiler run IS a normal team run (202; the worker drives it;
the caller polls the existing ``/v1/engine/team-runs/{id}`` reads). No new runtime, no new gateway
route family beyond this assembler.
"""

from __future__ import annotations

from typing import Any

from oraclous_governance import Principal
from oraclous_ohm.compiler import build_compiler_team
from oraclous_ohm.manifest import OHMModel

from oraclous_execution_engine_service.domain.compiler_onramp import (
    compose_objective,
    draft_catalog,
)
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    TeamRunService,
)


class CompilerRunService:
    def __init__(self, *, team_runs: TeamRunService) -> None:
        self._team_runs = team_runs

    async def create(
        self,
        principal: Principal,
        *,
        objective: str,
        inputs: dict[str, Any] | None = None,
        constraints: str | None = None,
        success_criteria: str | None = None,
        models: list[dict[str, Any]],
        graph_id: str | None = None,
    ) -> EngineTeamRun:
        if principal.organisation_id is None:  # fail-closed tenancy (ADR-006)
            raise TeamRunError("authenticated principal has no organisation scope", 403)
        org = principal.organisation_id
        bound_models = validate_model_bindings(models, who="a compiler run")
        composed = compose_objective(
            objective,
            inputs=inputs,
            constraints=constraints,
            success_criteria=success_criteria,
        )
        manifest, subs = build_compiler_team(org, objective=composed, catalog=draft_catalog())
        doc = manifest.model_dump(mode="json")
        doc["models"] = bound_models
        bound_subs = {role: {**sub, "models": bound_models} for role, sub in subs.items()}
        return await self._team_runs.create(
            principal,
            manifest=doc,
            sub_harnesses=bound_subs,
            gate_decisions={},
            graph_id=graph_id,
        )


def validate_model_bindings(
    models: list[dict[str, Any]] | None, *, who: str
) -> list[dict[str, Any]]:
    """The caller's BYOM model bindings (OHMModel-shaped; ``config.credential_id`` inside) —
    validated at the edge so a malformed binding is a curated 422, not a mid-run member failure.
    Shared by the compiler on-ramp and refine-nl (#635) — both submit LLM members."""
    if not models:
        raise TeamRunError(
            f"{who} needs models[] (its members are real LLM agents — BYOM)",
            422,
            error_type="missing_models",
        )
    try:
        return [OHMModel.model_validate(m).model_dump(mode="json") for m in models]
    except Exception as exc:  # noqa: BLE001 — pydantic detail stays server-side (leak-safe)
        raise TeamRunError(
            "models[] entries must be OHM model bindings (role/binding/protocol_shape/config)",
            422,
            error_type="invalid_models",
        ) from exc
