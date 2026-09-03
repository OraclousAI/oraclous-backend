"""Compiler-run service (services layer) — the Describe door's server side (#635, C-1(a)).

``POST /v1/engine/compiler-runs`` assembles the harness-compiler team server-side
(``build_compiler_team`` — ADR-047's compiler-as-a-team, previously constructible only by
``packages/ohm``), bakes the #596 seeded catalog into the manifest-drafter's subgoal (#709 deleted
the capability-surveyor step), binds the caller's
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
    draft_catalog_described,
)
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.services.registry_client import (
    RegistryClient,
    RegistryClientError,
)
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    TeamRunService,
)


async def surveyed_catalog(registry: RegistryClient | None) -> list[str]:
    """The surveyed draft catalog for the caller's org (#638): the #596 seed inventory UNIONed with
    the org's LIVE registry capability names. A registry outage — or no client wired (the unit
    path) — degrades to seed-only: strictly fail-closed (a live tool is never admitted un-surveyed;
    an unregistered tool always rejects). One seam, every catalog consumer (the compiler survey,
    the assemble/refine validation, the op-drafter survey text)."""
    registered: list[str] = []
    if registry is not None:
        try:
            registered = await registry.list_capabilities()
        except RegistryClientError:  # unreachable / non-2xx → seed-only, never fail-open
            registered = []
    return draft_catalog(registered)


async def surveyed_catalog_described(
    registry: RegistryClient | None,
) -> list[dict[str, str]]:
    """The same surveyed catalog, plus what each tool DOES — the menu, for a PROMPT.

    #713: choosing a tool from a slug alone is close to guessing from the name. In compiler run
    ``a3443e24`` the drafter handed ``knowledge-retriever`` to the member whose job was reading an
    unmerged pull request diff; the tool reads the org's stored documents and could only ever
    return nothing for that job. The gate has nothing to say about it — the tool is registered,
    active and first-party — so the fix belongs in what the drafter is shown.

    Returns one entry per catalog slug, in the same order as ``surveyed_catalog``, carrying
    ``description`` ONLY when the registry has one. A seed-inventory tool has no descriptor row and
    so no description; it renders as its name alone rather than an invented blurb. Degrades exactly
    like ``surveyed_catalog`` — a registry outage yields the seed catalog, undescribed, never
    fail-open."""
    rows: list[dict[str, str]] = []
    if registry is not None:
        try:
            rows = await registry.list_capability_rows()
        except RegistryClientError:  # unreachable / non-2xx → seed-only, never fail-open
            rows = []
    return draft_catalog_described(rows)


class CompilerRunService:
    def __init__(
        self, *, team_runs: TeamRunService, registry: RegistryClient | None = None
    ) -> None:
        self._team_runs = team_runs
        self._registry = registry  # #638: live-registry union into the compiler survey catalog

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
        # #638: seed ∪ live registry (org-scoped). #709/#713: the capability-surveyor step is gone
        # — the drafter reads the described catalog straight from its own sub-goal, and the
        # reviewer's manifest-validate reads the org's live registry directly, so only the
        # described view is needed here.
        described = await surveyed_catalog_described(self._registry)
        manifest, subs = build_compiler_team(
            org, objective=composed, catalog_descriptions=described
        )
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
