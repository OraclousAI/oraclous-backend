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
    OHMRunIf,
    OHMRuntime,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
# the SURVEYED catalog — the ceiling a refine may draw from. A second entry (#750) lets
# set_tools tests prove REPLACE, not merge: swap to a genuinely different surveyed tool.
_CATALOG = ["web-search", "doc-search"]


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
    # #750: SetTools and RemoveMember are new discriminated-union members, imported function-locally
    # per the seam-import rule — the union does not carry them yet, so this hard-fails RED (an
    # ImportError, or a ValidationError from the unrecognised "op" discriminator) until it does.
    from oraclous_ohm.compiler.refine import RemoveMember, SetTools

    assert isinstance(parse_op({"op": "add_member", "role": "qa"}), AddMember)
    assert isinstance(parse_op({"op": "set_fan_out", "role": "r", "over": "$.x"}), SetFanOut)
    assert isinstance(parse_op({"op": "change_kind", "role": "e", "kind": "human"}), ChangeKind)
    assert isinstance(
        parse_op({"op": "add_depends_on", "role": "w", "depends_on": "r"}), AddDependsOn
    )
    assert isinstance(parse_op({"op": "set_tools", "role": "r", "tools": ["web-search"]}), SetTools)
    assert isinstance(parse_op({"op": "remove_member", "role": "r"}), RemoveMember)


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


# ── #750: set_tools — REPLACES the tools list wholesale, prunes tool_rationale ───────────────


def test_set_tools_replaces_the_list_and_preserves_the_rest() -> None:
    from oraclous_ohm.compiler.refine import SetTools

    m = _manifest()
    res = apply_refine(
        m,
        SetTools(role="writer", tools=["doc-search"]),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is not None and res.report.would_block is False
    writer = {x.role: x for x in res.manifest.members}["writer"]
    assert writer.tools == ["doc-search"]
    _assert_preserved(m, res.manifest, except_roles={"writer"})


def test_set_tools_replaces_rather_than_merges() -> None:
    # the member starts with ONE tool; the op sets a DIFFERENT one — a merge would keep both.
    m = _manifest()
    assert m.members[0].tools == ["web-search"]  # researcher's starting tool (the fixture)
    from oraclous_ohm.compiler.refine import SetTools

    res = apply_refine(
        m,
        SetTools(role="researcher", tools=["doc-search"]),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is not None and res.report.would_block is False
    researcher = {x.role: x for x in res.manifest.members}["researcher"]
    assert researcher.tools == ["doc-search"]  # EXACTLY the op's list — "web-search" is gone


def test_set_tools_to_empty_removes_every_tool() -> None:
    from oraclous_ohm.compiler.refine import SetTools

    m = _manifest()
    res = apply_refine(
        m, SetTools(role="researcher", tools=[]), catalog=_CATALOG, owner_organization_id=_ORG
    )
    assert res.manifest is not None and res.report.would_block is False
    researcher = {x.role: x for x in res.manifest.members}["researcher"]
    assert researcher.tools == []


def test_set_tools_prunes_and_updates_the_tool_rationale() -> None:
    from oraclous_ohm.compiler.refine import SetTools

    m = _manifest()
    by_role = {x.role: x for x in m.members}
    by_role["researcher"].tool_rationale = {"web-search": "needed to gather background"}
    res = apply_refine(
        m,
        SetTools(
            role="researcher",
            tools=["doc-search"],
            tool_rationale={"doc-search": "needed to check prior documentation"},
        ),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is not None and res.report.would_block is False
    researcher = {x.role: x for x in res.manifest.members}["researcher"]
    assert researcher.tools == ["doc-search"]
    # the stale "web-search" rationale entry is PRUNED — it no longer names a held tool
    assert researcher.tool_rationale == {"doc-search": "needed to check prior documentation"}


def test_set_tools_with_an_unsurveyed_tool_fails_closed() -> None:
    from oraclous_ohm.compiler.refine import SetTools

    m = _manifest()
    res = apply_refine(
        m,
        SetTools(role="researcher", tools=["definitely-not-a-real-tool"]),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is None and res.report.would_block is True
    assert any("F-CAPABILITY-MISSING" in b for b in res.report.blocking)


@pytest.mark.security
def test_set_tools_cannot_escalate_capability() -> None:
    # SECURITY: mirrors test_a_refine_cannot_escalate_capability for set_tools — an NL edit that
    # tries to grant a send/publish/spend tool the surveyor never offered must NOT escalate
    # capability. It blocks, and the caller's manifest is left UNMUTATED.
    from oraclous_ohm.compiler.refine import SetTools

    m = _manifest()
    original_tools = list(m.members[0].tools)
    assert m.members[0].role == "researcher"
    res = apply_refine(
        m,
        SetTools(role="researcher", tools=["send-to-drafts"]),  # NOT surveyed
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is None and res.report.would_block is True
    assert any("F-CAPABILITY-MISSING" in b for b in res.report.blocking)
    # the original is untouched — researcher's tools were never escalated
    assert m.members[0].tools == original_tools


@pytest.mark.security
def test_set_tools_on_an_unknown_member_fails_closed() -> None:
    from oraclous_ohm.compiler.refine import SetTools

    m = _manifest()
    res = apply_refine(
        m,
        SetTools(role="ghost", tools=["web-search"]),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
    )
    assert res.manifest is None and res.report.would_block is True
    assert any("F-REFINE-UNKNOWN-MEMBER" in b for b in res.report.blocking)


def test_set_tools_naming_a_file_tool_under_graph_substrate_blocks_with_substrate_reason() -> None:
    # #750: apply_refine gains a keyword-only ``substrate`` param (default "graph"), matching
    # validate_draft's existing signature. Calling it here — a NEW kwarg the current signature does
    # not accept — hard-fails RED with a TypeError until the [impl] adds it.
    from oraclous_ohm.compiler.refine import SetTools

    m = _manifest()
    res = apply_refine(
        m,
        SetTools(role="researcher", tools=["write"]),
        catalog=_CATALOG,
        owner_organization_id=_ORG,
        substrate="graph",
    )
    assert res.manifest is None and res.report.would_block is True
    assert any("F-SUBSTRATE-FILE" in b for b in res.report.blocking)
    # the substrate reason takes PRECEDENCE — never also reported as a plain capability miss
    assert not any("F-CAPABILITY-MISSING" in b for b in res.report.blocking)


# ── #750: remove_member — refuses rather than orphans (dependant / entrypoint / loop) ────────


def test_remove_member_deletes_it_and_preserves_the_rest() -> None:
    from oraclous_ohm.compiler.refine import RemoveMember

    m = _manifest()  # researcher (entrypoint) -> writer -> editor (a leaf, nobody depends on it)
    res = apply_refine(m, RemoveMember(role="editor"), catalog=_CATALOG, owner_organization_id=_ORG)
    assert res.manifest is not None and res.report.would_block is False
    assert {x.role for x in res.manifest.members} == {"researcher", "writer"}
    _assert_preserved(m, res.manifest, except_roles={"editor"})


def test_remove_member_refuses_while_a_dependant_exists() -> None:
    from oraclous_ohm.compiler.refine import RemoveMember

    m = _manifest()  # editor depends_on=["writer"]
    res = apply_refine(m, RemoveMember(role="writer"), catalog=_CATALOG, owner_organization_id=_ORG)
    assert res.manifest is None and res.report.would_block is True
    assert any("F-REFINE-MEMBER-DEPENDED-ON" in b and "editor" in b for b in res.report.blocking)


def test_remove_member_names_every_dependant_in_the_message() -> None:
    from oraclous_ohm.compiler.refine import RemoveMember

    m = _manifest()  # editor already depends_on=["writer"]
    m.members.append(
        OHMMember(
            role="fact-checker", kind="agent", manifest_ref="org:x/f@1", depends_on=["writer"]
        )
    )
    res = apply_refine(m, RemoveMember(role="writer"), catalog=_CATALOG, owner_organization_id=_ORG)
    assert res.manifest is None and res.report.would_block is True
    msg = next(b for b in res.report.blocking if "F-REFINE-MEMBER-DEPENDED-ON" in b)
    assert "editor" in msg and "fact-checker" in msg
    assert msg.index("editor") < msg.index("fact-checker")  # sorted


def test_remove_member_refuses_while_a_run_if_points_at_it() -> None:
    from oraclous_ohm.compiler.refine import RemoveMember

    m = _manifest()
    # "notifier" is dispatched purely via run_if.from_role — NOT depends_on — so this pins the
    # run_if half of the guard independently of the depends_on half (test above).
    m.members.append(
        OHMMember(
            role="notifier",
            kind="agent",
            manifest_ref="org:x/n@1",
            depends_on=["researcher"],
            run_if=OHMRunIf(from_role="writer"),
        )
    )
    res = apply_refine(m, RemoveMember(role="writer"), catalog=_CATALOG, owner_organization_id=_ORG)
    assert res.manifest is None and res.report.would_block is True
    assert any("F-REFINE-MEMBER-DEPENDED-ON" in b and "notifier" in b for b in res.report.blocking)


def test_remove_member_refuses_the_runtime_entrypoint() -> None:
    """#750 — without this guard the draft is written successfully and then becomes permanently
    unreadable: ``apply_refine`` replaces only ``members`` and leaves ``runtime.entrypoint``
    dangling; ``assemble_team`` computes its own entrypoint and never notices; the next
    ``load_ohm`` raises and every later GET/PUT/refine/run on the stored draft 422s forever."""
    from oraclous_ohm.compiler.refine import RemoveMember

    # isolated: "starter" is the entrypoint with NO dependant, so only F-REFINE-ENTRYPOINT can fire.
    m = OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=[
            OHMMember(role="starter", kind="agent", manifest_ref="org:x/s@1"),
            OHMMember(role="helper", kind="agent", manifest_ref="org:x/h@1"),
        ],
        runtime=OHMRuntime(entrypoint="starter"),
    )
    res = apply_refine(
        m, RemoveMember(role="starter"), catalog=_CATALOG, owner_organization_id=_ORG
    )
    assert res.manifest is None and res.report.would_block is True
    assert any("F-REFINE-ENTRYPOINT" in b for b in res.report.blocking)
    assert not any("F-REFINE-MEMBER-DEPENDED-ON" in b for b in res.report.blocking)


def test_remove_member_refuses_a_member_inside_a_loop_seam() -> None:
    from oraclous_ohm.compiler.refine import RemoveMember

    # isolated: "looped" is neither the entrypoint nor anyone's dependant, so only
    # F-REFINE-MEMBER-IN-LOOP can fire.
    m = OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=[
            OHMMember(role="starter", kind="agent", manifest_ref="org:x/s@1"),
            OHMMember(role="looped", kind="agent", manifest_ref="org:x/l@1"),
        ],
        runtime=OHMRuntime(entrypoint="starter"),
        orchestration=OHMOrchestration(loops=[OHMLoop(members=["looped"])]),
    )
    res = apply_refine(m, RemoveMember(role="looped"), catalog=_CATALOG, owner_organization_id=_ORG)
    assert res.manifest is None and res.report.would_block is True
    assert any("F-REFINE-MEMBER-IN-LOOP" in b for b in res.report.blocking)


def test_remove_member_of_an_unknown_role_fails_closed() -> None:
    from oraclous_ohm.compiler.refine import RemoveMember

    m = _manifest()
    res = apply_refine(m, RemoveMember(role="ghost"), catalog=_CATALOG, owner_organization_id=_ORG)
    assert res.manifest is None and res.report.would_block is True
    assert any("F-REFINE-UNKNOWN-MEMBER" in b for b in res.report.blocking)


def test_remove_the_last_member_fails_closed() -> None:
    # already covered by the existing F-NO-MEMBERS path (import_/setup.py) — pinned here too so a
    # future refactor of remove_member cannot silently regress it.
    from oraclous_ohm.compiler.refine import RemoveMember

    m = OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=[OHMMember(role="solo", kind="agent", manifest_ref="org:x/s@1")],
        runtime=OHMRuntime(entrypoint="solo"),
    )
    res = apply_refine(m, RemoveMember(role="solo"), catalog=_CATALOG, owner_organization_id=_ORG)
    assert res.manifest is None and res.report.would_block is True
    assert any("F-NO-MEMBERS" in b for b in res.report.blocking)


def test_remove_member_leaves_the_returned_entrypoint_resolvable() -> None:
    """Regression for the permanent-brick finding (#750): before the F-REFINE-ENTRYPOINT guard, a
    remove_member of a NON-entrypoint leaf still had to leave the returned manifest genuinely
    loadable — ``load_ohm`` must not raise on it. This is the proof that a refined-and-applied
    manifest never becomes the 422-forever draft the finding describes."""
    from oraclous_ohm.compiler.refine import RemoveMember
    from oraclous_ohm.parse import load_ohm

    m = _manifest()
    res = apply_refine(m, RemoveMember(role="editor"), catalog=_CATALOG, owner_organization_id=_ORG)
    assert res.manifest is not None and res.report.would_block is False
    reloaded = load_ohm(res.manifest.model_dump(mode="json"))  # must NOT raise
    assert reloaded.runtime.entrypoint == "researcher"


def test_a_blocked_removal_does_not_mutate_the_input() -> None:
    from oraclous_ohm.compiler.refine import RemoveMember

    m = _manifest()
    before_roles = {x.role for x in m.members}
    res = apply_refine(m, RemoveMember(role="writer"), catalog=_CATALOG, owner_organization_id=_ORG)
    assert res.manifest is None and res.report.would_block is True
    assert {x.role for x in m.members} == before_roles  # unmutated
