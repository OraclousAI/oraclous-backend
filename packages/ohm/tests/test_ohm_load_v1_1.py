"""The real loader (``load_ohm``) accepts OHM v1.1 team manifests (issue #402).

#394 added the v1.1 schema to ``OHMManifest`` but its tests used ``model_validate`` directly and
bypassed ``load_ohm`` — which gated on ``1.0`` and only cross-checked ``actors``/``capabilities``.
These tests drive the *real* loader: v1.1 loads, a team ``entrypoint`` resolves against members, and
a structurally-invalid team (bad entrypoint / cycle / duplicate role) is rejected fail-closed.
"""

from __future__ import annotations

import uuid

import pytest

from oraclous_ohm.errors import OHMSchemaError, OHMVersionError
from oraclous_ohm.parse import load_ohm


def _v10() -> dict:
    return {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": "a", "owner_organization_id": str(uuid.uuid4())},
        "capabilities": [{"ref": "core/echo@1", "binding": "echo"}],
        "runtime": {"entrypoint": "echo"},
    }


def _team(entrypoint: str = "researcher", members: list[dict] | None = None) -> dict:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "t",
            "owner_organization_id": str(uuid.uuid4()),
            "kind": "team",
        },
        "members": members
        or [
            {"role": "researcher", "kind": "agent", "manifest_ref": "org:x/r@1", "depends_on": []},
            {"role": "editor", "kind": "human", "human_role": "lead", "depends_on": ["researcher"]},
        ],
        "runtime": {"entrypoint": entrypoint},
    }


def test_v10_manifest_still_loads() -> None:
    m = load_ohm(_v10())
    assert m.ohm_version == "1.0"
    assert m.is_team() is False


def test_v11_team_manifest_loads_through_the_real_loader() -> None:
    m = load_ohm(_team())
    assert m.is_team() is True
    assert m.ohm_version == "1.1"
    assert m.execution_stages() == [["researcher"], ["editor"]]


def test_team_entrypoint_resolves_against_a_member_role() -> None:
    assert load_ohm(_team(entrypoint="editor")).runtime.entrypoint == "editor"


def test_team_entrypoint_matching_nothing_is_rejected() -> None:
    with pytest.raises(OHMSchemaError):
        load_ohm(_team(entrypoint="ghost"))


def test_unsupported_version_is_rejected() -> None:
    bad = _v10()
    bad["ohm_version"] = "2.0"
    with pytest.raises(OHMVersionError):
        load_ohm(bad)


def test_cyclic_team_dag_rejected_at_load() -> None:
    members = [
        {"role": "a", "kind": "agent", "manifest_ref": "o:x/a@1", "depends_on": ["b"]},
        {"role": "b", "kind": "agent", "manifest_ref": "o:x/b@1", "depends_on": ["a"]},
    ]
    with pytest.raises(OHMSchemaError):
        load_ohm(_team(entrypoint="a", members=members))


def test_duplicate_member_role_rejected_at_load() -> None:
    members = [
        {"role": "a", "kind": "agent", "manifest_ref": "o:x/a@1"},
        {"role": "a", "kind": "agent", "manifest_ref": "o:x/a2@1"},
    ]
    with pytest.raises(OHMSchemaError):
        load_ohm(_team(entrypoint="a", members=members))
