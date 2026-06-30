"""#595 (ADR-047 §4) — NL refine as a typed structural delta: the four ops, re-validated through the
SAME gate, with the load-bearing PRESERVE-THE-REST byte-identity invariant + the reject/fail-closed
paths (cycle, unsurveyed tool, human-without-role, capability escalation).
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.compiler.refine import (
    AddDependsOn,
    AddMember,
    ChangeKind,
    SetFanOut,
    apply_refine,
    parse_op,
)
from oraclous_ohm.manifest import (
    OHMLoop,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMOrchestration,
    OHMRuntime,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_CATALOG = ["web-search"]  # the SURVEYED catalog — the ceiling a refine may draw from


def _manifest() -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=[
            OHMMember(
                role="researcher", kind="agent", manifest_ref="org:x/r@1", tools=["web-search"]
            ),
            OHMMember(
                role="writer", kind="agent", manifest_ref="org:x/w@1", depends_on=["researcher"]
            ),
            OHMMember(role="editor", kind="agent", manifest_ref="org:x/e@1", depends_on=["writer"]),
        ],
        runtime=OHMRuntime(entrypoint="researcher"),
    )


def _by_role(m: OHMManifest) -> dict[str, dict]:
    return {x.role: x.model_dump(mode="json") for x in m.members}


def _assert_preserved(before: OHMManifest, after: OHMManifest, *, except_roles: set[str]) -> None:
    b, a = _by_role(before), _by_role(after)
    for role in b:
        if role not in except_roles:
            assert a[role] == b[role], f"member {role!r} was NOT preserved byte-identical"


def test_add_member_lands_and_preserves_the_rest() -> None:
    m = _manifest()
    res = apply_refine(
        m,
        AddMember(role="fact-checker", tools=["web-search"], depends_on=["researcher"]),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is not None and res.report.would_block is False
    by = {x.role: x for x in res.manifest.members}
    assert by["fact-checker"].tools == ["web-search"] and by["fact-checker"].depends_on == [
        "researcher"
    ]
    _assert_preserved(m, res.manifest, except_roles=set())  # nothing pre-existing changed


def test_set_fan_out_lands_and_preserves_the_rest() -> None:
    m = _manifest()
    res = apply_refine(
        m,
        SetFanOut(role="researcher", over="$.topics", max_parallel=3),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is not None and res.report.would_block is False
    fo = {x.role: x for x in res.manifest.members}["researcher"].fan_out
    assert fo is not None and fo.over == "$.topics" and fo.max_parallel == 3
    _assert_preserved(m, res.manifest, except_roles={"researcher"})


def test_change_kind_to_human_with_role_lands_and_preserves_the_rest() -> None:
    m = _manifest()
    res = apply_refine(
        m,
        ChangeKind(role="editor", kind="human", human_role="copy editor"),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is not None and res.report.would_block is False
    ed = {x.role: x for x in res.manifest.members}["editor"]
    assert ed.kind == "human" and ed.human_role == "copy editor"
    _assert_preserved(m, res.manifest, except_roles={"editor"})


def test_add_depends_on_lands_and_preserves_the_rest() -> None:
    m = _manifest()
    res = apply_refine(
        m,
        AddDependsOn(role="editor", depends_on="researcher"),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is not None and res.report.would_block is False
    ed = {x.role: x for x in res.manifest.members}["editor"]
    assert "researcher" in ed.depends_on
    _assert_preserved(m, res.manifest, except_roles={"editor"})


def test_change_kind_to_human_without_role_fails_closed() -> None:
    m = _manifest()
    res = apply_refine(
        m, ChangeKind(role="editor", kind="human"), catalog=_CATALOG, owner_organization_id=_ORG
    )
    assert res.manifest is None and res.report.would_block is True  # human requires human_role


def test_add_depends_on_that_cycles_is_rejected_not_mutated() -> None:
    m = _manifest()
    # researcher → writer → editor; making researcher depend on editor closes a cycle
    res = apply_refine(
        m,
        AddDependsOn(role="researcher", depends_on="editor"),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is None and res.report.would_block is True  # OHMDagError → blocking


def test_add_member_with_an_unsurveyed_tool_fails_closed() -> None:
    m = _manifest()
    res = apply_refine(
        m,
        AddMember(role="rogue", tools=["delete-everything"], depends_on=["researcher"]),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is None and res.report.would_block is True
    assert any("F-CAPABILITY-MISSING" in b for b in res.report.blocking)


@pytest.mark.security
def test_a_refine_cannot_escalate_capability() -> None:
    # SECURITY: an NL edit that tries to grant a send/publish/spend tool the surveyor never offered
    # must NOT escalate capability — it blocks, and the manifest is left UNMUTATED.
    m = _manifest()
    res = apply_refine(
        m,
        AddMember(role="exfiltrator", tools=["send-to-drafts"], depends_on=["writer"]),
        catalog=_CATALOG,  # send-to-drafts is NOT surveyed
        owner_organization_id=_ORG,
    )
    assert res.manifest is None and res.report.would_block is True
    assert any("F-CAPABILITY-MISSING" in b for b in res.report.blocking)
    # the original is untouched — no rogue member, no escalated tool
    assert {x.role for x in m.members} == {"researcher", "writer", "editor"}


def test_an_op_on_an_unknown_member_fails_closed() -> None:
    m = _manifest()
    res = apply_refine(
        m,
        ChangeKind(role="ghost", kind="human", human_role="x"),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is None and res.report.would_block is True


def test_add_member_duplicate_role_fails_closed() -> None:
    m = _manifest()
    res = apply_refine(
        m, AddMember(role="writer", tools=[]), catalog=_CATALOG, owner_organization_id=_ORG
    )
    assert res.manifest is None and res.report.would_block is True


def test_parse_op_routes_the_discriminated_union() -> None:
    assert isinstance(parse_op({"op": "add_member", "role": "qa"}), AddMember)
    assert isinstance(parse_op({"op": "set_fan_out", "role": "r", "over": "$.x"}), SetFanOut)
    assert isinstance(parse_op({"op": "change_kind", "role": "e", "kind": "human"}), ChangeKind)
    assert isinstance(
        parse_op({"op": "add_depends_on", "role": "w", "depends_on": "r"}), AddDependsOn
    )


def test_parse_op_peels_prose_wrapped_json() -> None:
    # the op-drafter LLM wraps the op in prose / a ```json fence — it must still parse (#599)
    text = 'Sure! Here is the edit:\n```json\n{"op": "add_member", "role": "fact-checker"}\n```'
    op = parse_op(text)
    assert isinstance(op, AddMember) and op.role == "fact-checker"


def test_parse_op_rejects_a_malformed_op() -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_op({"op": "delete_everything", "role": "x"})  # not one of the four typed ops
    with pytest.raises(ValueError):
        parse_op("no json here at all")


def test_preserve_the_rest_covers_orchestration_and_does_not_mutate_input() -> None:
    # HIGH regression (adversarial review): the assembler reassigns orchestration.loops IN PLACE, so
    # apply_refine must deep-copy it — never mutate the caller's manifest nor drop a loop-bearing
    # team's coordinator seam on the success path.
    m = _manifest()
    m.orchestration = OHMOrchestration(loops=[OHMLoop(members=["researcher", "writer"])])
    before = m.orchestration.model_dump(mode="json")
    res = apply_refine(
        m,
        AddDependsOn(role="editor", depends_on="researcher"),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is not None and res.report.would_block is False
    assert res.manifest.orchestration is not None
    assert res.manifest.orchestration.model_dump(mode="json") == before  # loops preserved
    assert m.orchestration.model_dump(mode="json") == before  # input NOT mutated


def test_a_blocked_refine_does_not_mutate_the_input_orchestration() -> None:
    m = _manifest()
    m.orchestration = OHMOrchestration(loops=[OHMLoop(members=["researcher", "writer"])])
    before = m.orchestration.model_dump(mode="json")
    res = apply_refine(  # a cycle → blocked
        m,
        AddDependsOn(role="researcher", depends_on="editor"),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is None and res.report.would_block is True
    assert (
        m.orchestration.model_dump(mode="json") == before
    )  # input untouched on the blocked path too


def test_change_kind_human_to_agent_clears_the_stale_human_role() -> None:
    m = _manifest()
    res1 = apply_refine(
        m,
        ChangeKind(role="editor", kind="human", human_role="copy editor"),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res1.manifest is not None
    res2 = apply_refine(
        res1.manifest,
        ChangeKind(role="editor", kind="agent"),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res2.manifest is not None and res2.report.would_block is False
    ed = {x.role: x for x in res2.manifest.members}["editor"]
    assert ed.kind == "agent" and ed.human_role is None  # no stale human_role lingers
