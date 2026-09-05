"""App service (services layer, #932) — the Apps tab's behaviour.

An app is a team behind a short form. This layer does four things and deliberately no more: it lists
what a caller can see (their own apps AND the Oraclous-provided ones), opens one, says what still
has to be connected before it will run, and runs it on the caller's own key.

There is no create, update or delete here. Turning a team into an app is deferred by the owner's
ruling — not every team is an app, and the conversion needs designing before it is built. Until
then the only apps that exist are the ones Oraclous seeds.
"""

from __future__ import annotations

import uuid
from typing import Any

from oraclous_governance import Principal

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.domain.apps import (
    bind_run_documents,
    derive_origin,
    form_fields,
)
from oraclous_execution_engine_service.models.app import EngineApp
from oraclous_execution_engine_service.models.team_run import EngineTeamRun
from oraclous_execution_engine_service.repositories.app_repository import AppRepository
from oraclous_execution_engine_service.repositories.team_run_repository import TeamRunRepository
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    TeamRunService,
    load_team_manifest,
)

#: Only the caller's own key is accepted today. "owner" — the app's owner pays — needs credential
#: delegation, a billing-attribution answer and an operator-separation review (CLAUDE.md §3.6), so
#: the field exists on the record and the value is refused until that work happens.
_SUPPORTED_CREDENTIALS_MODES = frozenset({"caller"})


class AppService:
    def __init__(
        self,
        *,
        apps: AppRepository,
        team_runs: TeamRunService,
        team_run_repository: TeamRunRepository,
        platform_org_id: uuid.UUID,
    ) -> None:
        self._apps = apps
        self._team_runs = team_runs
        self._team_run_repository = team_run_repository
        self._platform_org_id = platform_org_id

    def _org(self, principal: Principal) -> uuid.UUID:
        if principal.organisation_id is None:  # fail-closed tenancy (ADR-006)
            raise TeamRunError("authenticated principal has no organisation scope", 403)
        return principal.organisation_id

    def _origin(self, row: EngineApp) -> str:
        return derive_origin(row.organisation_id, platform_org_id=self._platform_org_id)

    async def _get_or_404(self, app_id: uuid.UUID, org: uuid.UUID) -> EngineApp:
        row = await self._apps.get(app_id, org)
        if row is None:
            # A cross-organisation app is ABSENT, never forbidden — a 403 would confirm that some
            # other organisation owns an app by this id (ADR-006, the team-draft posture).
            raise TeamRunError("app not found", 404)
        return row

    async def list_for_org(
        self, principal: Principal, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """The Apps tab: the caller's own apps and the Oraclous-provided ones, in one page.

        One list rather than two endpoints, so the console renders the split from ``origin`` rather
        than from which call a row arrived on.
        """
        org = self._org(principal)
        bounded_limit = max(1, min(limit, 200))  # born bounded (WP-10)
        bounded_offset = max(0, offset)
        with org_scope(org):
            rows, total = await self._apps.list_for_org(
                org, limit=bounded_limit, offset=bounded_offset
            )
        for row in rows:
            row["origin"] = derive_origin(
                row["organisation_id"], platform_org_id=self._platform_org_id
            )
        return rows, total

    async def get(self, app_id: uuid.UUID, principal: Principal) -> dict[str, Any]:
        """One app, opened: where it came from, and the form to fill in.

        Deliberately does NOT return the team documents. A person running an app is not opening the
        plan behind it — that is the whole difference between an app and a team — and a platform
        app's documents are shared, so publishing them by default would widen what is exposed for
        no one's benefit.
        """
        org = self._org(principal)
        with org_scope(org):
            row = await self._get_or_404(app_id, org)
        return self._as_detail(row)

    async def get_by_slug(self, slug: str, principal: Principal) -> dict[str, Any]:
        """The deep-link read. The console finds the Validation Desk by its stable handle instead
        of the uuid that used to be pasted into a frontend environment variable."""
        org = self._org(principal)
        with org_scope(org):
            row = await self._apps.get_by_slug(slug, org)
        if row is None:
            raise TeamRunError("app not found", 404)
        return self._as_detail(row)

    def _as_detail(self, row: EngineApp) -> dict[str, Any]:
        return {
            "id": row.id,
            "origin": self._origin(row),
            "name": row.name,
            "description": row.description,
            "slug": row.slug,
            "inputs": form_fields(row.manifest),
            "member_count": len(row.manifest.get("members") or []),
            "pinned_version": row.pinned_version,
            "credentials_mode": row.credentials_mode,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    async def requirements(self, app_id: uuid.UUID, principal: Principal) -> dict[str, Any]:
        """What this app needs, and what the caller has not connected yet.

        Asks the SAME computation the GO gate asks (``missing_tool_credentials``), so the page and
        the gate can never disagree — a screen that says "ready" over a run that then 409s is worse
        than no screen at all.

        The model is always required and never satisfied in advance: a stored app carries no
        credential, so the caller's key arrives with the run request rather than being connected
        ahead of it.
        """
        org = self._org(principal)
        with org_scope(org):
            row = await self._get_or_404(app_id, org)

        team = load_team_manifest(row.manifest)
        missing = await self._team_runs.missing_tool_credentials(team, row.sub_harnesses)
        unmet = {(m["role"], m["binding"]) for m in missing}

        tools: list[dict[str, Any]] = []
        for member in team.members:
            for binding in member.tools or []:
                key = (member.role, binding)
                found = next(
                    (m for m in missing if (m["role"], m["binding"]) == key),
                    None,
                )
                tools.append(
                    {
                        "role": member.role,
                        "binding": binding,
                        "provider": (found or {}).get("provider", binding),
                        "credential_type": (found or {}).get("credential_type"),
                        "satisfied": key not in unmet,
                    }
                )

        models = row.manifest.get("models") or []
        binding = models[0].get("binding") if models and isinstance(models[0], dict) else None
        return {
            "credentials_mode": row.credentials_mode,
            "model_required": True,
            "model_binding": binding,
            "tools": tools,
            # "ready" is about the TOOLS only: the model key travels with the run request, so it is
            # never something the caller can have connected in advance.
            "ready": not missing,
        }

    async def run(
        self,
        app_id: uuid.UUID,
        principal: Principal,
        *,
        inputs: dict[str, Any] | None,
        models: list[dict[str, Any]] | None,
        graph_id: str | None = None,
        workspace_root: str | None = None,
    ) -> EngineTeamRun:
        """Run an app on the CALLER's own model key.

        The app's stored documents are copied and rebound before anything is dispatched — the
        caller's model onto the team and every member, the caller's organisation onto the metadata
        — and the copy is what runs. The stored row is untouched, which matters because a platform
        app's row is shared by every organisation that can see it.

        Everything after that is an ordinary team run: the same validators refuse an input key the
        team cannot read, and the same pre-flight raises the same 409 when a tool is unconnected.
        """
        org = self._org(principal)
        with org_scope(org):
            row = await self._get_or_404(app_id, org)

        if row.credentials_mode not in _SUPPORTED_CREDENTIALS_MODES:
            raise TeamRunError(
                f"this app is set to {row.credentials_mode!r} credentials, which is not available "
                "yet — a run currently uses the caller's own model key",
                422,
                error_type="credentials_mode_unsupported",
            )

        run_manifest, run_subs = bind_run_documents(
            row.manifest, row.sub_harnesses, models=models, organisation_id=org
        )
        return await self._team_runs.create(
            principal,
            manifest=run_manifest,
            sub_harnesses=run_subs,
            # No pre-seeded gate decisions: an app's runner never saw the plan, so they are in no
            # position to pre-approve one of its human gates. A gated team pauses and waits, exactly
            # as it would for anyone else.
            gate_decisions={},
            inputs=inputs,
            graph_id=graph_id,
            workspace_root=workspace_root,
            app_id=row.id,
        )

    async def list_runs(
        self, app_id: uuid.UUID, principal: Principal, *, limit: int = 20, offset: int = 0
    ) -> tuple[list[Any], int]:
        """This app's runs — STRICTLY the caller's organisation.

        The app may be shared; its runs never are. Two organisations running the same
        Oraclous-provided app must not see each other's inputs or results, so this read carries no
        widening even though the app read above does.
        """
        org = self._org(principal)
        bounded_limit = max(1, min(limit, 200))
        with org_scope(org):
            await self._get_or_404(app_id, org)  # a cross-org app id is a 404 here too
            return await self._team_run_repository.list_for_app(
                org, app_id, limit=bounded_limit, offset=max(0, offset)
            )
