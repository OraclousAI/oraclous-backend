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
from oraclous_ohm.compiler.validate import _catalog_slugs, _tool_slug
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


#: a discriminated union — the LLM op-drafter emits exactly ONE of these (function-calling shape).
RefineOp = Annotated[AddMember | SetFanOut | ChangeKind | AddDependsOn, Field(discriminator="op")]

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


def _capability_absence_flags(members: list[OHMMember], catalog: object) -> list[ImportFlag]:
    """The ADR-032 gate, identical to the compiler's: every member.tools entry must be a SURVEYED
    tool — an unsurveyed/empty-slug tool blocks F-CAPABILITY-MISSING so a refine cannot ESCALATE
    capability (grant an undeclared send/publish/spend tool the surveyor never offered)."""
    allowed = _catalog_slugs(catalog)
    flags: list[ImportFlag] = []
    for m in members:
        for tool in m.tools:
            slug = _tool_slug(tool)
            if not slug or slug not in allowed:
                flags.append(
                    ImportFlag(
                        code="F-CAPABILITY-MISSING",
                        severity="blocking",
                        member_role=m.role,
                        message=f"tool {tool!r} is not in the surveyed capability catalog",
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
            )
        except ValueError as exc:  # e.g. a human added without a human_role → fail closed
            return [_flag("F-REFINE-INVALID-MEMBER", op.role, str(exc))]
        members.append(new)
        by_role[new.role] = new
        return []

    target = by_role.get(op.role)
    if target is None:
        return [_flag("F-REFINE-UNKNOWN-MEMBER", op.role, f"no member named {op.role!r}")]

    if isinstance(op, SetFanOut):
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
) -> RefineResult:
    """Apply a typed NL-refine op to ``manifest`` and re-validate through the SAME gate the importer
    and compiler use. Returns the patched manifest (only the named member changed, everything else
    byte-identical) + the dry-run report; on a blocking delta the manifest is None (NOT mutated) and
    the report carries the gap reasons (``would_block=True``)."""
    patched_members = [m.model_copy(deep=True) for m in manifest.members]
    by_role = {m.role: m for m in patched_members}

    flags = _apply_op(op, patched_members, by_role)
    flags += _capability_absence_flags(patched_members, catalog)
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
