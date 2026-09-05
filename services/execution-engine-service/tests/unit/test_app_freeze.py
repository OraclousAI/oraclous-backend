"""An app's frozen documents (#932) — the copy every organisation may read.

An app carries its OWN copy of the team's documents rather than a pointer to a draft, for three
reasons that are all facts about the shipped code: a draft's old versions are not retained
(``replace``/``refine`` overwrite in place), a saved draft since #695/ADR-050 D3 keeps only its
members' ``manifest_ref``s to registry agents that are editable in place, and a platform-owned app
cannot point at a draft living in another organisation.

That copy is readable by EVERY organisation (the platform-org widened read, ADR-006's
platform-catalogue case). So the one thing freezing must guarantee is that **nothing secret and
nothing org-specific survives into it**: not the author's model credential id, not the author's
tool credential mappings. A credential id is org-scoped at the broker, so a leaked one is useless
to another tenant — but it is still one organisation's identifier sitting in a row every other
organisation can read, and that is the leak, not the exploitability.

RED until ``domain/app_freeze.py`` lands. The seam is imported function-locally on purpose
(`.claude/rules/tests-seam-imports.md`): a module-level import of a not-yet-built ``oraclous_*``
seam aborts collection for the whole run and reddens every open PR.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = [pytest.mark.unit]


def _model(credential_id: str) -> dict[str, Any]:
    return {
        "role": "primary",
        "binding": "openrouter/deepseek/deepseek-v3.2",
        "protocol_shape": "openai-compatible",
        "config": {"credential_id": credential_id, "temperature": 0.2},
    }


def _team() -> dict[str, Any]:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": "24869709-764a-49d0-b259-ce561340a8ec",
            "name": "desk-research-team",
            "owner_organization_id": "00000000-0000-0000-0000-0000000000a0",
            "kind": "team",
        },
        "task_input": {"required": True, "key": "task", "description": "The idea to validate."},
        "models": [_model("aaaaaaaa-1111-1111-1111-111111111111")],
        "members": [
            {
                "role": "researcher",
                "kind": "agent",
                "manifest_ref": "org:desk/researcher@1",
                "subgoal": "gather evidence",
                "depends_on": [],
                "tools": ["web-research"],
                "tool_rationale": {"web-research": "searching is its whole job"},
                "outputs_schema": {"required": ["summary"]},
            }
        ],
        "runtime": {"entrypoint": "researcher"},
    }


def _sub_harnesses() -> dict[str, dict[str, Any]]:
    return {
        "researcher": {
            "ohm_version": "1.0",
            "metadata": {"id": "b1", "name": "researcher"},
            "models": [_model("bbbbbbbb-2222-2222-2222-222222222222")],
            "capabilities": [
                {
                    "ref": "web-research",
                    "binding": "web_research",
                    "config": {
                        "credential_mappings": {"api_key": "cccccccc-3333-3333-3333-333333333333"},
                        "max_results": 5,
                    },
                }
            ],
            "actors": [{"role": "primary", "kind": "agent"}],
            "runtime": {"entrypoint": "primary"},
        }
    }


def test_no_credential_identifier_survives_the_freeze() -> None:
    """The security contract, asserted over the WHOLE serialized document rather than field by
    field: whatever shape a future manifest grows, none of the author's credential identifiers may
    appear anywhere in what other organisations read."""
    from oraclous_execution_engine_service.domain.app_freeze import freeze_documents

    frozen = freeze_documents(_team(), _sub_harnesses())

    serialized = json.dumps({"m": frozen.manifest, "s": frozen.sub_harnesses})
    for secret in (
        "aaaaaaaa-1111-1111-1111-111111111111",
        "bbbbbbbb-2222-2222-2222-222222222222",
        "cccccccc-3333-3333-3333-333333333333",
    ):
        assert secret not in serialized
    assert "credential_id" not in serialized
    assert "credential_mappings" not in serialized


def test_the_model_binding_survives_so_the_app_can_say_what_it_runs_on() -> None:
    """Stripping the credential must not strip the MODEL. The app screen says 'built for
    openrouter/deepseek-v3.2', and the run rebinds the caller's own key onto that same binding —
    so losing the binding would lose the app's identity as well as its key."""
    from oraclous_execution_engine_service.domain.app_freeze import freeze_documents

    frozen = freeze_documents(_team(), _sub_harnesses())

    team_model = frozen.manifest["models"][0]
    assert team_model["binding"] == "openrouter/deepseek/deepseek-v3.2"
    assert team_model["role"] == "primary"
    assert team_model["protocol_shape"] == "openai-compatible"
    # config survives MINUS the credential — an unrelated setting is not collateral damage
    assert team_model.get("config", {}).get("temperature") == 0.2

    sub_model = frozen.sub_harnesses["researcher"]["models"][0]
    assert sub_model["binding"] == "openrouter/deepseek/deepseek-v3.2"


def test_a_capability_keeps_everything_except_its_credential_mapping() -> None:
    """The tool binding is what the run's credential pre-flight resolves against, so it stays. Only
    the author's instance credential mapping goes."""
    from oraclous_execution_engine_service.domain.app_freeze import freeze_documents

    frozen = freeze_documents(_team(), _sub_harnesses())

    capability = frozen.sub_harnesses["researcher"]["capabilities"][0]
    assert capability["ref"] == "web-research"
    assert capability["binding"] == "web_research"
    assert capability["config"]["max_results"] == 5
    assert "credential_mappings" not in capability["config"]


def test_freezing_does_not_mutate_the_documents_it_was_handed() -> None:
    """The caller's documents are a live draft the rest of the request still reads. Freezing copies;
    it never edits in place."""
    from oraclous_execution_engine_service.domain.app_freeze import freeze_documents

    manifest, subs = _team(), _sub_harnesses()
    freeze_documents(manifest, subs)

    assert (
        manifest["models"][0]["config"]["credential_id"] == "aaaaaaaa-1111-1111-1111-111111111111"
    )
    mappings = subs["researcher"]["capabilities"][0]["config"]["credential_mappings"]
    assert mappings == {"api_key": "cccccccc-3333-3333-3333-333333333333"}


def test_the_fingerprint_ignores_key_order_so_a_reseed_is_a_no_op() -> None:
    """The startup seed re-runs on every boot and must not rewrite an unchanged app. It compares
    fingerprints, so the fingerprint has to be stable under a reordering that changes nothing."""
    from oraclous_execution_engine_service.domain.app_freeze import freeze_documents

    manifest = _team()
    reordered = json.loads(json.dumps(manifest, sort_keys=True))
    assert list(reordered) != list(manifest)  # the fixture really is reordered

    assert (
        freeze_documents(manifest, _sub_harnesses()).fingerprint
        == freeze_documents(reordered, _sub_harnesses()).fingerprint
    )


def test_a_changed_document_changes_the_fingerprint() -> None:
    """The other half: a real edit must be detected, or a re-seed would silently keep stale
    documents."""
    from oraclous_execution_engine_service.domain.app_freeze import freeze_documents

    edited = _team()
    edited["members"][0]["subgoal"] = "gather evidence, and say what it does not cover"

    assert (
        freeze_documents(_team(), _sub_harnesses()).fingerprint
        != freeze_documents(edited, _sub_harnesses()).fingerprint
    )


def test_a_credential_buried_deeper_than_the_known_shapes_is_still_removed() -> None:
    """Fail closed on shape drift. The two known homes are ``models[].config`` and
    ``capabilities[].config``, but a manifest that grows a third one must not quietly start
    publishing credentials — so the scrub is driven by the KEY, wherever it sits."""
    from oraclous_execution_engine_service.domain.app_freeze import freeze_documents

    manifest = _team()
    manifest["runtime"]["fallback"] = {"config": {"credential_id": "dddddddd-4444-4444-4444-4444"}}

    frozen = freeze_documents(manifest, _sub_harnesses())
    assert "dddddddd-4444-4444-4444-4444" not in json.dumps(frozen.manifest)
