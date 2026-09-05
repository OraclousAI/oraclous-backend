"""Binding a caller's own model onto an app's frozen documents before the run (#932).

The owner's ruling: a default app runs on the CALLER's model key, never an Oraclous-owned one. The
design enforces that rather than asking for it — freezing strips every credential id, and the seed
never had one, so a platform app's stored documents cannot start a run on anybody's key. The caller
supplies ``models`` per run and the engine binds them onto a copy, exactly as the compiler on-ramp
already does (``compiler_run_service`` binds a caller-supplied ``models`` onto the document and each
sub-harness). The engine still never holds the key itself — only a credential id the caller owns
(ADR-008).

The organisation rewrite is the other half, and it is load-bearing rather than cosmetic: nothing in
``TeamRunService.create`` validates ``metadata.owner_organization_id`` against the caller, it is
simply propagated into every synthesized member document. A platform app run without the rewrite
would carry the platform organisation into a tenant's run.

RED until ``domain/apps.py`` lands; the seam is imported function-locally
(`.claude/rules/tests-seam-imports.md`).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = [pytest.mark.unit]

PLATFORM_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a0")
CALLER_ORG = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

CALLER_MODEL = {
    "role": "primary",
    "binding": "openrouter/deepseek/deepseek-v3.2",
    "protocol_shape": "openai-compatible",
    "config": {"credential_id": "cafe0000-0000-0000-0000-000000000001"},
}


def _frozen_team() -> dict[str, Any]:
    """A platform app's stored manifest: a model BINDING with no credential, and the platform org
    stamped as its owner — which is what a frozen row really looks like."""
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "desk-research-team",
            "owner_organization_id": str(PLATFORM_ORG),
            "kind": "team",
        },
        "task_input": {"required": True, "key": "task", "description": "The idea to validate."},
        "models": [
            {
                "role": "primary",
                "binding": "openrouter/deepseek/deepseek-v3.2",
                "protocol_shape": "openai-compatible",
                "config": {},
            }
        ],
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:desk/researcher@1",
                "subgoal": "gather evidence",
                "depends_on": [],
                "outputs_schema": {"required": ["summary"]},
            }
        ],
        "runtime": {"entrypoint": "researcher"},
    }


def _frozen_subs() -> dict[str, dict[str, Any]]:
    return {
        "researcher": {
            "ohm_version": "1.0",
            "metadata": {"id": "b1", "name": "researcher"},
            "models": [{"role": "primary", "binding": "openrouter/x", "config": {}}],
            "capabilities": [],
            "actors": [{"role": "primary", "kind": "agent"}],
            "runtime": {"entrypoint": "primary"},
        }
    }


def test_the_callers_model_is_bound_on_the_team_and_on_every_member() -> None:
    """Both places matter. The team-level binding is what a reader of the app sees and what the
    desk's intake reads (#927); the per-member binding is what each member actually runs on."""
    from oraclous_execution_engine_service.domain.apps import bind_run_documents

    manifest, subs = bind_run_documents(
        _frozen_team(), _frozen_subs(), models=[CALLER_MODEL], organisation_id=CALLER_ORG
    )

    assert manifest["models"] == [CALLER_MODEL]
    assert subs["researcher"]["models"] == [CALLER_MODEL]


def test_the_run_carries_the_callers_organisation_not_the_platforms() -> None:
    from oraclous_execution_engine_service.domain.apps import bind_run_documents

    manifest, subs = bind_run_documents(
        _frozen_team(), _frozen_subs(), models=[CALLER_MODEL], organisation_id=CALLER_ORG
    )

    assert manifest["metadata"]["owner_organization_id"] == str(CALLER_ORG)
    assert str(PLATFORM_ORG) not in str(manifest)


def test_binding_leaves_the_stored_app_documents_untouched() -> None:
    """The frozen row is shared by every organisation that can see the app. If a run bound onto it
    in place, one tenant's credential would land in another tenant's read."""
    from oraclous_execution_engine_service.domain.apps import bind_run_documents

    stored_manifest, stored_subs = _frozen_team(), _frozen_subs()
    bind_run_documents(
        stored_manifest, stored_subs, models=[CALLER_MODEL], organisation_id=CALLER_ORG
    )

    assert stored_manifest["models"][0]["config"] == {}
    assert stored_subs["researcher"]["models"][0]["config"] == {}
    assert stored_manifest["metadata"]["owner_organization_id"] == str(PLATFORM_ORG)


def test_a_run_with_no_model_is_refused() -> None:
    """A frozen app carries no credential, so a run with no supplied model has no key at all. That
    must fail at create with a named reason, not reach a member and die mid-run."""
    from oraclous_execution_engine_service.domain.apps import bind_run_documents
    from oraclous_execution_engine_service.services.team_run_service import TeamRunError

    with pytest.raises(TeamRunError) as excinfo:
        bind_run_documents(_frozen_team(), _frozen_subs(), models=[], organisation_id=CALLER_ORG)

    assert excinfo.value.status_code == 422
    assert excinfo.value.error_type == "missing_models"


def test_a_malformed_model_is_refused_with_the_shared_validators_verdict() -> None:
    """The engine already validates caller-supplied model bindings for the compiler on-ramp and for
    the plain-English refine. An app run reuses that verdict rather than growing a second one, so a
    caller sees one error vocabulary across every place they hand the platform a model."""
    from oraclous_execution_engine_service.domain.apps import bind_run_documents
    from oraclous_execution_engine_service.services.team_run_service import TeamRunError

    with pytest.raises(TeamRunError) as excinfo:
        bind_run_documents(
            _frozen_team(),
            _frozen_subs(),
            models=[{"role": "primary"}],  # no binding
            organisation_id=CALLER_ORG,
        )

    assert excinfo.value.status_code == 422
    assert excinfo.value.error_type == "invalid_models"
