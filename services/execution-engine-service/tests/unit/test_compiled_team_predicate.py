"""#1169 — the compile-run predicate (domain layer).

A build run that succeeds on a retry is never saved as a team, because the console only saves it
when its own tab is still open. The fix saves the compiled team at settle time — but ONLY for a
compile run, never for an ordinary user team or the op-drafter / app-form-drafter one-member teams
the engine also runs. ``is_compile_run(manifest)`` is the pure decision: plain dict in, bool out.

True iff ``metadata.name == "harness-compiler"`` AND some member has ``role == "reviewer"`` and
``manifest_ref == "org:compiler/reviewer@1"``. Both halves are required: the name alone is a user's
to choose, the reviewer member alone could appear in any team.

The compiler shape comes from the real ``build_compiler_team`` (packages/ohm), dumped the way
``compiler_run_service`` dumps it, so this file cannot drift from the shape a real build run has.

RED until ``domain/compiled_team.py`` exists; the seam is imported function-locally per
``.claude/rules/tests-seam-imports.md`` and a missing module must fail, never skip.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
from oraclous_ohm.compiler import build_compiler_team
from oraclous_ohm.manifest import OHMManifest, OHMMember, OHMMetadata, OHMRuntime

pytestmark = [pytest.mark.unit]

_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _compiler_doc() -> dict[str, Any]:
    manifest, _subs = build_compiler_team(_ORG, objective="a team that triages support tickets")
    return manifest.model_dump(mode="json")


def _one_member_team(name: str, role: str, manifest_ref: str) -> dict[str, Any]:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name=name, owner_organization_id=_ORG, kind="team"),
        members=[OHMMember(role=role, kind="agent", manifest_ref=manifest_ref, subgoal="x")],
        runtime=OHMRuntime(entrypoint=role),
    ).model_dump(mode="json")


def test_the_real_compiler_team_is_a_compile_run() -> None:
    from oraclous_execution_engine_service.domain.compiled_team import is_compile_run

    assert is_compile_run(_compiler_doc()) is True


def test_the_op_drafter_team_is_not_a_compile_run() -> None:
    from oraclous_execution_engine_service.domain.compiled_team import is_compile_run

    doc = _one_member_team("refine-op-drafter", "op-drafter", "org:refine/op-drafter@1")

    assert is_compile_run(doc) is False


def test_the_app_form_drafter_team_is_not_a_compile_run() -> None:
    from oraclous_execution_engine_service.domain.compiled_team import is_compile_run

    doc = _one_member_team("app-form-drafter", "drafter", "org:app-form/drafter@1")

    assert is_compile_run(doc) is False


def test_an_ordinary_user_team_is_not_a_compile_run() -> None:
    from oraclous_execution_engine_service.domain.compiled_team import is_compile_run

    doc = _one_member_team("support-triage", "triager", "org:support/triager@1")

    assert is_compile_run(doc) is False


def test_the_compiler_name_without_a_reviewer_member_is_not_a_compile_run() -> None:
    from oraclous_execution_engine_service.domain.compiled_team import is_compile_run

    doc = _compiler_doc()
    doc["members"] = [m for m in doc["members"] if m["role"] != "reviewer"]
    assert doc["members"], "the fixture must still have members left"

    assert is_compile_run(doc) is False


def test_a_reviewer_role_with_a_different_manifest_ref_is_not_a_compile_run() -> None:
    from oraclous_execution_engine_service.domain.compiled_team import is_compile_run

    doc = _compiler_doc()
    for member in doc["members"]:
        if member["role"] == "reviewer":
            member["manifest_ref"] = "org:someone-else/reviewer@1"

    assert is_compile_run(doc) is False


def test_the_compiler_reviewer_ref_under_a_different_team_name_is_not_a_compile_run() -> None:
    from oraclous_execution_engine_service.domain.compiled_team import is_compile_run

    doc = _compiler_doc()
    doc["metadata"]["name"] = "my-own-team"

    assert is_compile_run(doc) is False


def test_the_compiler_ref_on_a_member_with_another_role_is_not_a_compile_run() -> None:
    from oraclous_execution_engine_service.domain.compiled_team import is_compile_run

    doc = _compiler_doc()
    for member in doc["members"]:
        if member["role"] == "reviewer":
            member["role"] = "checker"

    assert is_compile_run(doc) is False


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"metadata": {"name": "harness-compiler"}},
        {"members": [{"role": "reviewer", "manifest_ref": "org:compiler/reviewer@1"}]},
        {"metadata": {}, "members": []},
        {"metadata": {"name": "harness-compiler"}, "members": [{}]},
        {"metadata": None, "members": None},
    ],
    ids=[
        "empty",
        "no-members-key",
        "no-metadata-key",
        "empty-metadata-and-members",
        "member-missing-keys",
        "null-values",
    ],
)
def test_a_manifest_missing_keys_is_false_and_never_raises(doc: dict[str, Any]) -> None:
    from oraclous_execution_engine_service.domain.compiled_team import is_compile_run

    assert is_compile_run(doc) is False


def test_it_is_a_pure_function_of_a_plain_dict_and_does_not_mutate_it() -> None:
    from oraclous_execution_engine_service.domain.compiled_team import is_compile_run

    doc = _compiler_doc()
    before = copy.deepcopy(doc)

    first = is_compile_run(doc)
    second = is_compile_run(doc)

    assert first is True
    assert second is True
    assert doc == before
