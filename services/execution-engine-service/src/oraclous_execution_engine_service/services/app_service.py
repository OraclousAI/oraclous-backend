"""App service (services layer, #932/#938) — the Apps tab's behaviour.

An app is a team behind a short form. This layer lists what a caller can see (their own apps AND
the Oraclous-provided ones), opens one, says what still has to be connected before it will run,
runs it on the caller's own key — and, since #938, turns one of the caller's own finished team runs
into a new app.

There is still no update or delete (the owner's ruling on #938 scoped this to create only; a third
issue owns the rest of the lifecycle). Renaming, re-pointing or removing an app is out of scope
here.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from oraclous_governance import Principal

from oraclous_execution_engine_service.core.rls import org_scope
from oraclous_execution_engine_service.domain.app_form import (
    FormField,
    FormShapeError,
    fallback_fields,
    fan_out_keys,
    missing_required,
    parse_form_draft,
    to_run_inputs,
)
from oraclous_execution_engine_service.domain.apps import (
    app_slug,
    app_slug_candidates,
    bind_run_documents,
    derive_origin,
    form_fields,
    plan_summary,
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
            # The AUTHORED form (#938): a model drafted it, a person edited it. NULL for an app
            # that predates this column — every seeded platform app included — which falls back to
            # the #932 single-field projection rather than coming back empty.
            "form": (
                row.form
                if row.form is not None
                else [asdict(f) for f in fallback_fields(row.manifest)]
            ),
            # What will happen and what it can cost — structure only, never a member's prompt.
            "plan": plan_summary(row.manifest),
            "member_count": len(row.manifest.get("members") or []),
            "pinned_version": row.pinned_version,
            "credentials_mode": row.credentials_mode,
            # The owner's ruling on #877: this column, not a new boolean, is the whole signal that
            # tells a converted app apart from a hand-made one. None for every platform app.
            "source_team_run_id": row.source_team_run_id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    async def create_from_run(
        self,
        principal: Principal,
        *,
        team_run_id: uuid.UUID,
        name: str,
        description: str | None,
        fields: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Turn one of the caller's own finished team runs into an app (#938).

        One run, that SUCCEEDED — the ruling is a product one: every app should be something its
        author watched work. The frozen copy is a direct read of the run's OWN documents
        (``run.manifest`` + ``run.sub_harnesses``), not the draft that produced them — a draft's
        old versions are not retained, so there is nothing else to freeze from.

        Returns ``(detail, created)`` — the SAME shape a read returns, so the console can open the
        new app immediately; ``created`` is ``False`` when this run already has an app (no delete
        endpoint exists in this issue, so a double-submitted save must not leave a duplicate nobody
        can remove).
        """
        org = self._org(principal)
        with org_scope(org):
            run = await self._team_run_repository.get(team_run_id, org)
        if run is None:
            # Not a 403 — confirming a run exists but belongs to another organisation is an
            # enumeration the engine's other reads already refuse to make.
            raise TeamRunError("team run not found", 404)
        if run.state != "SUCCEEDED":
            raise TeamRunError(
                f"an app is made from a run its author watched work (state {run.state})",
                422,
                error_type="run_not_succeeded",
            )

        # An empty list is accepted here, unlike in a model's DRAFT. A team that declares no
        # input has nothing for a person to fill in, and the domain already treats that as a
        # legitimate end state — ``fallback_fields`` offers none and the fold handles none. The
        # save has to agree, or a run whose read correctly offers an empty form cannot be
        # converted at all (code review).
        parsed_fields: list[FormField] = []
        if fields:
            try:
                parsed_fields = parse_form_draft({"fields": fields})
            except FormShapeError as exc:
                raise TeamRunError(str(exc), 422, error_type="invalid_form") from exc
        names = [f.name for f in parsed_fields]
        if len(names) != len(set(names)):
            # The draft parser forgives a chatty model; the person's own edit is an authoring
            # mistake worth naming while they can still fix it — there is no update endpoint.
            raise TeamRunError(
                "two fields cannot share a name", 422, error_type="duplicate_field_name"
            )

        with org_scope(org):
            existing = await self._apps.get_by_source_run(team_run_id, org)
        if existing is not None:
            return self._as_detail(existing), False

        chosen_slug: str | None = None
        base = app_slug(name)
        if base is not None:
            with org_scope(org):
                for candidate in app_slug_candidates(base):
                    if not await self._apps.slug_exists(candidate, org):
                        chosen_slug = candidate
                        break

        with org_scope(org):
            row = await self._apps.create(
                organisation_id=org,
                user_id=principal.principal_id,
                name=name,
                description=description,
                slug=chosen_slug,
                manifest=run.manifest,
                sub_harnesses=run.sub_harnesses,
                source_team_run_id=run.id,
                form=[asdict(f) for f in parsed_fields],
            )
        return self._as_detail(row), True

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

        An app carrying a stored form (#938) folds the caller's ``inputs`` into the team's one
        declared key BEFORE anything else happens — a required field left blank is refused here,
        never handed to the run path, so nothing is created and nothing is spent. An app with no
        stored form (``row.form is None`` — every app that predates #938) keeps today's behaviour
        exactly: ``inputs`` reaches the run path untouched.
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

        run_inputs = inputs
        if row.form is not None:
            fields = [FormField(**f) for f in row.form]
            values = inputs or {}
            missing = missing_required(fields, values)
            if missing:
                raise TeamRunError(
                    "fill in before running this app: " + ", ".join(missing),
                    422,
                    error_type="missing_required_field",
                )
            # The fold covers the drafted fields; a fan-out key is a list the person supplies
            # and travels as itself. Passing nothing here dropped it silently (code review).
            carried = {k: values[k] for k in fan_out_keys(row.manifest) if k in values}
            run_inputs = to_run_inputs(row.manifest, fields, values, passthrough=carried)

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
            inputs=run_inputs,
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
