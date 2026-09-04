"""#595 (ADR-047 §4) — NL review/edit refine as a TYPED STRUCTURAL DELTA on a compiled team.

After the compiler (#594) turns prose into a runnable OHM v1.1 Team Harness, the user refines it in
natural language — "add a fact-checker", "make research parallel", "the editor is human", "the
writer depends on the researcher". The model emits the PATCH (one of four typed ops), NOT a whole
new manifest (the small typed-edit surface is the function-calling-shaped problem LLMs are reliable
at; alternative F "blank re-draft" rejected). The op is applied to a DEEP COPY and the patched
manifest is re-run through the SAME ``assemble_and_report`` dry-run the importer and compiler use
(#593, one validator two on-ramps) — a delta that cycles the DAG, references an unsurveyed tool, or
flips a member to ``human`` without a ``human_role`` is rejected with a CODED ``would_block`` + a
gap report, never silently applied.

THE PRESERVE-THE-REST INVARIANT (the load-bearing contract): only the member the op names changes;
every other member is byte-identical (``model_dump(mode="json")``) before vs after. ``apply_refine``
guarantees it structurally — it deep-copies, mutates only the named member, and returns the original
manifest with just ``members`` replaced (NOT the assembler's transformed output).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# the SAME slug normalization the compiler's capability-absence gate uses (one implementation): a
# foreign namespace / emoji / nested path can never masquerade as a surveyed bare tool (#594).
from oraclous_ohm._slug import FILE_SUBSTRATE_TOOLS
from oraclous_ohm.compiler.validate import (
    DEFAULT_OUTPUTS_SCHEMA,
    Substrate,
    _catalog_slugs,
    _file_substrate_flag,
    _name_hint,
    _tool_slug,
)
from oraclous_ohm.dag import OHMDagError, topological_stages
from oraclous_ohm.import_ import ImportFlag, ImportReport, assemble_and_report
from oraclous_ohm.manifest import OHMFanOut, OHMManifest, OHMMember


class _BaseOp(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: str = Field(min_length=1)  # the member the op adds (add_member) or targets (the rest)


class AddMember(_BaseOp):
    """ "add a fact-checker" — append a member with SURVEYED tools[] + an acyclic depends_on."""

    op: Literal["add_member"] = "add_member"
    kind: Literal["agent", "human"] = "agent"
    tools: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    subgoal: str | None = None
    human_role: str | None = None
    # #697: the edit has nowhere for the user to state output keys, and since the compile gate
    # requires them, a member added without one would make the validation the refine endpoint
    # re-runs reject a team the user just asked for. The op-drafter MAY fill this; when it does
    # not, the applier supplies the same default the drafter emits.
    outputs_schema: dict[str, Any] = Field(default_factory=dict)
    manifest_ref: str | None = None


class SetFanOut(_BaseOp):
    """ "make research parallel" — set/replace the named member's fan_out."""

    op: Literal["set_fan_out"] = "set_fan_out"
    over: str = Field(min_length=1)
    max_parallel: int = Field(default=1, ge=1)
    reduce: str = "concat"


class ChangeKind(_BaseOp):
    """ "the editor is human" — flip the named member's kind; human REQUIRES human_role."""

    op: Literal["change_kind"] = "change_kind"
    kind: Literal["agent", "human"]
    human_role: str | None = None


class AddDependsOn(_BaseOp):
    """ "the writer depends on the researcher" — append a dependency edge, re-validated acyclic."""

    op: Literal["add_depends_on"] = "add_depends_on"
    depends_on: str = Field(min_length=1)  # the role the named member now waits on


class SetTools(_BaseOp):
    """ "give the researcher just web search" — REPLACES the named member's tools[] wholesale.

    Not a merge — the op restates the whole list the member should hold. ``tool_rationale`` is
    pruned to the tools actually held after the replace, then the op's entries are applied over the
    survivors: a kept tool's existing justification survives even when the op restates none."""

    op: Literal["set_tools"] = "set_tools"
    tools: list[str] = Field(default_factory=list)
    tool_rationale: dict[str, str] = Field(default_factory=dict)


class RemoveMember(_BaseOp):
    """ "drop the fact-checker" — delete the named member (#750).

    Refused, never silently orphaning, when the member is depended on (``depends_on`` or
    ``run_if.from_role``), is the runtime entrypoint, or sits in an orchestration loop seam.
    ``fan_out.over`` is a free-text JSONPath, not a typed role edge — it is deliberately NOT
    scanned for a role reference; guessing a role out of it would refuse legitimate removals."""

    op: Literal["remove_member"] = "remove_member"


#: a discriminated union — the LLM op-drafter emits exactly ONE of these (function-calling shape).
RefineOp = Annotated[
    AddMember | SetFanOut | ChangeKind | AddDependsOn | SetTools | RemoveMember,
    Field(discriminator="op"),
]

_OP_ADAPTER: TypeAdapter[RefineOp] = TypeAdapter(RefineOp)


def parse_op(data: dict[str, Any] | str) -> RefineOp:
    """Parse an op-drafter's output into exactly ONE typed ``RefineOp`` (the ``op`` key routes the
    discriminated union). Accepts a dict OR the LLM's text — the JSON object is PEELED out of the
    model's prose / ```json fence (#599), so a valid op wrapped in chatter still parses. Raises on a
    malformed / unknown op (the caller fails closed)."""
    if isinstance(data, str):
        match = re.search(r"\{.*\}", data, re.DOTALL)
        if match is None:
            raise ValueError("no JSON op object found in the draft")
        data = json.loads(match.group(0))
    return _OP_ADAPTER.validate_python(data)


class RefineResult(BaseModel):
    """The patched manifest (None if the delta is blocked) + the dry-run report (the gap report)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    manifest: OHMManifest | None = None
    report: ImportReport


def _capability_absence_flags(
    members: list[OHMMember], catalog: object, *, substrate: Substrate = "graph"
) -> list[ImportFlag]:
    """The ADR-032 gate, identical to the compiler's: every member.tools entry must be a SURVEYED
    tool — an unsurveyed/empty-slug tool blocks F-CAPABILITY-MISSING so a refine cannot ESCALATE
    capability (grant an undeclared send/publish/spend tool the surveyor never offered).

    #750: under the graph substrate a FILE tool (write/edit/read/grep/glob) blocks
    F-SUBSTRATE-FILE instead — the SAME precedence and wording ``validate_draft`` uses — since a
    file tool is not merely unsurveyed, it targets a substrate the graph default withholds
    entirely (the engine's ``draft_catalog`` never offers it, so it always fails the plain
    membership check too; the substrate reason is just the truthful one)."""
    allowed = _catalog_slugs(catalog)
    flags: list[ImportFlag] = []
    for m in members:
        for tool in m.tools:
            slug = _tool_slug(tool)
            if substrate == "graph" and slug in FILE_SUBSTRATE_TOOLS:
                flags.append(_file_substrate_flag(m.role, tool))
                continue
            if not slug or slug not in allowed:
                flags.append(
                    ImportFlag(
                        code="F-CAPABILITY-MISSING",
                        severity="blocking",
                        member_role=m.role,
                        # #899: the same hint as the compiler gate, from the same helper. This
                        # message was a verbatim copy of that one, and _slug.py's docstring is the
                        # record of what a second copy costs — two on-ramps disagreeing because
                        # neither could see the other's answer.
                        message=(
                            f"tool {tool!r} is not in the surveyed capability"
                            f" catalog.{_name_hint(slug, allowed)}"
                        ),
                    )
                )
    return flags


def _apply_op(
    op: RefineOp, members: list[OHMMember], by_role: dict[str, OHMMember]
) -> list[ImportFlag]:
    """Mutate ``members``/``by_role`` IN PLACE per the op; return any STRUCTURAL blocking flags
    (duplicate/unknown role). DAG cycles and human-without-role are NOT checked here — apply_refine
    catches those (``_dag_flags`` + the fail-closed assemble) as the single-validator path."""
    if isinstance(op, AddMember):
        if op.role in by_role:
            return [_flag("F-REFINE-DUP-ROLE", op.role, f"member {op.role!r} already exists")]
        try:
            new = OHMMember(
                role=op.role,
                kind=op.kind,
                tools=list(op.tools),
                depends_on=list(op.depends_on),
                subgoal=op.subgoal,
                human_role=op.human_role,
                manifest_ref=op.manifest_ref,
                outputs_schema=op.outputs_schema or dict(DEFAULT_OUTPUTS_SCHEMA),
            )
        except ValueError as exc:  # e.g. a human added without a human_role → fail closed
            return [_flag("F-REFINE-INVALID-MEMBER", op.role, str(exc))]
        members.append(new)
        by_role[new.role] = new
        return []

    target = by_role.get(op.role)
    if target is None:
        return [_flag("F-REFINE-UNKNOWN-MEMBER", op.role, f"no member named {op.role!r}")]

    if isinstance(op, RemoveMember):
        # cannot join the plain isinstance chain below — it must delete from BOTH members and the
        # by_role index the caller shares, not just mutate the target in place.
        def _depends_on_it(m: OHMMember) -> bool:
            return op.role in m.depends_on or (
                m.run_if is not None and m.run_if.from_role == op.role
            )

        dependants = sorted(m.role for m in members if m.role != op.role and _depends_on_it(m))
        if dependants:
            names = ", ".join(repr(r) for r in dependants)
            return [
                _flag(
                    "F-REFINE-MEMBER-DEPENDED-ON",
                    op.role,
                    f"member {op.role!r} cannot be removed — it is depended on by: {names}",
                )
            ]
        members[:] = [m for m in members if m.role != op.role]  # mutate IN PLACE
        del by_role[op.role]
        return []

    if isinstance(op, SetTools):
        # REPLACE wholesale, never merge (#750) — the caller's later _capability_absence_flags
        # pass, which walks ALL members post-mutation, is what makes replace-not-merge safe: a
        # replacement naming an unavailable tool blocks exactly as add_member does.
        target.tools = list(op.tools)
        held = {_tool_slug(t) for t in target.tools}
        # prune to tools actually held (by slug membership, NOT "was it named in this op") so a
        # kept tool's existing justification survives even when the op restates none.
        rationale = {k: v for k, v in target.tool_rationale.items() if _tool_slug(k) in held}
        rationale.update(op.tool_rationale)
        target.tool_rationale = rationale
    elif isinstance(op, SetFanOut):
        target.fan_out = OHMFanOut(over=op.over, max_parallel=op.max_parallel, reduce=op.reduce)
    elif isinstance(op, ChangeKind):
        target.kind = op.kind
        if op.kind == "human":
            if op.human_role is not None:
                target.human_role = op.human_role
        else:  # → agent: clear any stale human_role so an agent never carries a meaningless one
            target.human_role = None
    elif isinstance(op, AddDependsOn):
        if op.depends_on not in target.depends_on:
            target.depends_on.append(op.depends_on)
    return []


def _flag(code: str, role: str, message: str) -> ImportFlag:
    return ImportFlag(code=code, severity="blocking", member_role=role, message=message)


def _removal_guard_flags(manifest: OHMManifest, op: RefineOp) -> list[ImportFlag]:
    """Whole-manifest guards ``_apply_op`` cannot see (it gets only ``members``/``by_role``, never
    widened to take the whole manifest). Returns ``[]`` for any op that is not ``RemoveMember``.

    #750 — F-REFINE-ENTRYPOINT exists because, without it, removing the entrypoint member writes
    successfully and then bricks the draft permanently: ``apply_refine`` returns
    ``manifest.model_copy(update={"members": patched_members})`` — only ``members`` is replaced, so
    a dangling ``runtime.entrypoint`` survives. The dry-run does not notice (``assemble_team``
    computes its OWN entrypoint from the patched members and ignores the caller's); the next read
    raises in ``parse.load_ohm``, which the service layer turns into a 422 — forever, since every
    later read/edit/run on the stored draft hits the same raise.

    Both guards below run unconditionally — neither short-circuits the other, and neither
    short-circuits the dependants check in ``_apply_op`` — so a member that is both the entrypoint
    and depended on reports every reason at once."""
    if not isinstance(op, RemoveMember):
        return []
    flags: list[ImportFlag] = []
    if manifest.runtime.entrypoint == op.role:
        flags.append(
            _flag(
                "F-REFINE-ENTRYPOINT",
                op.role,
                f"member {op.role!r} is the runtime entrypoint and cannot be removed — point the"
                " entrypoint at another member first",
            )
        )
    for loop in manifest.orchestration.loops if manifest.orchestration else []:
        if op.role in loop.members:
            flags.append(
                _flag(
                    "F-REFINE-MEMBER-IN-LOOP",
                    op.role,
                    f"member {op.role!r} is part of an orchestration loop and cannot be removed",
                )
            )
            break
    return flags


def _dag_flags(op: RefineOp, members: list[OHMMember]) -> list[ImportFlag]:
    """The DAG check ``assemble_team``'s ``load_ohm`` round-trip does NOT run: topological_stages
    raises ``OHMDagError`` on a cycle / unknown ``depends_on`` / duplicate role (an
    ``add_depends_on`` that closes a loop). A cyclic manifest still CONSTRUCTS (the cycle only bites
    at run time), so the flag — not a crash — is what drives ``would_block``."""
    try:
        topological_stages(members)
    except OHMDagError as exc:
        return [_flag("F-REFINE-DAG", op.role, str(exc))]
    return []


def _blocked_report(name: str, flags: list[ImportFlag]) -> ImportReport:
    """A fail-closed report for a delta whose patched members cannot even assemble (a member-schema
    violation an op introduced) — built directly, never by re-assembling the invalid members."""
    return ImportReport(
        team_name=name,
        shape="compiled",
        blocking=[f"{f.code}: {f.message}" for f in flags if f.severity == "blocking"],
    )


def apply_refine(
    manifest: OHMManifest,
    op: RefineOp,
    *,
    catalog: object,
    owner_organization_id: uuid.UUID,
    substrate: Substrate = "graph",
) -> RefineResult:
    """Apply a typed NL-refine op to ``manifest`` and re-validate through the SAME gate the importer
    and compiler use. Returns the patched manifest (only the named member changed, everything else
    byte-identical) + the dry-run report; on a blocking delta the manifest is None (NOT mutated) and
    the report carries the gap reasons (``would_block=True``).

    ``substrate`` (#750) matches ``validate_draft``'s existing signature and defaults to ``graph``
    (ADR-040 Decision 7, cloud-first): a ``set_tools`` naming a file tool blocks
    ``F-SUBSTRATE-FILE`` instead of the generic capability-absence message."""
    patched_members = [m.model_copy(deep=True) for m in manifest.members]
    by_role = {m.role: m for m in patched_members}

    # whole-manifest removal guards (entrypoint / loop membership) run FIRST, before the mutation,
    # so the most actionable reason is listed first when a member is refused for more than one.
    flags = _removal_guard_flags(manifest, op)
    flags += _apply_op(op, patched_members, by_role)
    flags += _capability_absence_flags(patched_members, catalog, substrate=substrate)
    flags += _dag_flags(op, patched_members)  # cycle / unknown dep / dup role

    # deep-copy the orchestration into the assembler: ``assemble_team`` reassigns ``.loops`` on it
    # IN PLACE — passing ``manifest.orchestration`` by reference would mutate the caller's manifest
    # (on every path, including the blocked one) AND, since the refine passes no handoffs, recompute
    # ``loops`` to ``[]`` — silently dropping a loop-bearing team's coordinator seam. The copy keeps
    # the ORIGINAL intact, so the ``model_copy`` below preserves it (preserve-the-rest, unmutated).
    orch = manifest.orchestration.model_copy(deep=True) if manifest.orchestration else None
    try:
        result = assemble_and_report(
            manifest.metadata.name,
            patched_members,
            owner_organization_id=owner_organization_id,
            shape="compiled",
            orchestration=orch,
            extra_flags=flags,
        )
    except Exception as exc:  # noqa: BLE001 — FAIL CLOSED: an op that makes the members
        # unassemblable (change_kind→human without a human_role) blocks with a gap report, never a
        # crash or a silent apply.
        flags.append(_flag("F-REFINE-INVALID-MEMBER", op.role, str(exc)))
        return RefineResult(manifest=None, report=_blocked_report(manifest.metadata.name, flags))
    if result.report.would_block:
        return RefineResult(manifest=None, report=result.report)
    # return the ORIGINAL manifest with only members replaced — preserve-the-rest by construction
    # (NOT result.manifest, which the assembler may transform); the patched members validated.
    patched = manifest.model_copy(update={"members": patched_members}, deep=True)
    return RefineResult(manifest=patched, report=result.report)
