"""The reviewer's draft-validation GATE (#594, ADR-047 decisions 1 + 3).

The manifest-drafter emits its draft Team Harness as JSON text (a member's harness output is text —
#599). ``validate_draft`` lowers that JSON to ``members[]`` + ``orchestration``, DIFFS each member's
``tools[]`` against the SURVEYED catalog (a hallucinated/unsurveyed tool → a blocking
``F-CAPABILITY-MISSING`` — the assembler will NOT catch it on its own, it happily synthesises
``core/<slug>@1`` refs, so the diff MUST live here, ADR-032), and runs the SAME
``assemble_and_report`` dry-run the importer uses (one validator, two on-ramps). It returns a CODED
verdict — ``would_block`` is a deterministic boolean from the validator, not the reviewer's opinion,
so the reviewer's bounded re-draft converges on a fact, never self-certifies (ADR-043 invariant).
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any, Literal

from pydantic import ValidationError

from oraclous_ohm._slug import FILE_SUBSTRATE_TOOLS, tool_slug
from oraclous_ohm.import_ import ImportFlag, assemble_and_report, render_report
from oraclous_ohm.manifest import OHMMember, OHMOrchestration, OHMTaskInput

_UUID_NS = "00000000-0000-0000-0000-000000000000"

#: #697 — what a member declares when nothing more specific fits. The ruling left the default to
#: the implementer and said only: propose one, never emit ``{}``. ``summary`` is the one key every
#: member can always honour — it is the answer it was going to write anyway, under a name. A
#: richer default would be a liability, not a courtesy: ``validate_payload`` checks key PRESENCE,
#: so a declared key the model cannot fill is a fail-closed hand-off error.
DEFAULT_OUTPUTS_SCHEMA: dict[str, Any] = {"required": ["summary"]}


#: The canonical tool-name normaliser, now shared with ``seeds`` and ``import_.mapping`` from one
#: leaf module (#694). It was copied here and inlined in ``seeds`` because ``compiler.validate``
#: imports ``import_``; the copies drifted on CASE, which is the mechanism of #694. Kept under its
#: existing private name — callers and the #594 tests import ``_tool_slug`` — but it IS the shared
#: function now, never a look-alike.
_tool_slug = tool_slug

Substrate = Literal["graph", "file"]

#: The membership set lives in the ``_slug`` leaf, beside the normaliser that compares against it.
#: Under the graph substrate the catalog no longer offers these (``compiler_onramp.draft_catalog``),
#: so the drafter cannot choose one — but a draft can still carry one by another route: a hand edit,
#: a refine op, a team compiled before this slice, or a live-registry capability whose name
#: collides. This gate is what stops any of those reaching storage (#694).
_FILE_SUBSTRATE_TOOLS = FILE_SUBSTRATE_TOOLS


def _catalog_slugs(catalog: Any) -> set[str]:
    """The set of SURVEYED tool identifiers (slugged) the drafter is allowed to draw from. Accepts a
    list of bare names/refs OR of dicts ({name|binding|ref}) — whatever the survey tool returned. An
    entry that slugs to EMPTY is dropped (never a wildcard ``""`` that would let an empty-slug
    drafted tool slip through)."""
    out: set[str] = set()
    items = catalog.get("tools", catalog) if isinstance(catalog, dict) else catalog
    for it in items if isinstance(items, list) else []:
        candidates = [it] if isinstance(it, str) else []
        if isinstance(it, dict):
            candidates = [it[k] for k in ("binding", "name", "ref") if isinstance(it.get(k), str)]
        for val in candidates:
            slug = _tool_slug(val)
            if slug:  # drop empties so "" is never a wildcard match (MEDIUM hardening)
                out.add(slug)
    return out


# #899: how a blocked tool name names the nearest surveyed one. The member reading this verdict is
# a MODEL with two repair attempts, and #705 is the recorded cost of leaving it to guess — one name
# dropped while re-typing a 72-entry list blocked an entire compile. The neighbouring flags already
# learned this: F-SUBSTRATE-FILE names the tools to use instead (#694), and #751 made the schema
# failure name the field and the reason.
_MAX_SUGGESTED_TOOLS = 3
#: Below this similarity the match is a guess. A WRONG suggestion is worse than none: the member
#: takes it, and the gate blocks again for a new reason on the attempt it cannot spare.
_NAME_MATCH_CUTOFF = 0.6


def _name_hint(slug: str, allowed: set[str]) -> str:
    """`` Did you mean: …?``, or an empty string when nothing is close enough.

    Matches on the SLUG, never the raw string, because the gate itself compares slugs: ``Web
    Search`` and ``core/web-search@1`` are one tool here, and measuring the raw form would score a
    legitimate spelling of a surveyed tool as far from its own catalogue entry. A degenerate
    identifier slugs to ``""`` and matches nothing, which is correct — it should block with no
    suggestion, not be nudged toward the nearest real name.
    """
    near = difflib.get_close_matches(
        slug, sorted(allowed), n=_MAX_SUGGESTED_TOOLS, cutoff=_NAME_MATCH_CUTOFF
    )
    return f" Did you mean: {', '.join(near)}?" if near else ""


def _blocked(code: str, message: str) -> dict[str, Any]:
    """A fail-closed verdict for a draft we cannot even parse — never a crash (decision 3)."""
    import uuid

    flag = ImportFlag(code=code, severity="blocking", member_role="", message=message)
    result = assemble_and_report(
        "compiled-team",
        [],
        owner_organization_id=uuid.UUID(_UUID_NS),
        shape="compiled",
        extra_flags=[flag],
    )
    return {
        "would_block": True,
        "blocking": result.report.blocking,
        "report": render_report(result.report),
    }


def _member_label(raw: Any, index: int) -> str:
    """How to NAME a member that did not parse. Its own ``role`` when it has a usable one, else its
    position — because a member whose ``role`` is the broken field still has to be findable."""
    if isinstance(raw, dict):
        role = raw.get("role")
        if isinstance(role, str) and role.strip():
            return role
    return f"members[{index}]"


def _schema_detail(raw: Any, index: int, exc: ValidationError) -> str:
    """The Pydantic error, rendered for a MODEL that has to repair the member it names (#751).

    Keeps the two facts the reviewer needs and nothing else: which field, and what was wrong with
    it. The value is deliberately NOT echoed — a draft field can carry a tenant's own words."""
    parts: list[str] = []
    for err in exc.errors()[:5]:
        field = ".".join(str(loc) for loc in err.get("loc", ())) or "<member>"
        parts.append(f"{field}: {err.get('msg', 'is invalid')}")
    return f"member {_member_label(raw, index)} failed schema validation — " + "; ".join(parts)


def _blocked_with(flags: list[ImportFlag]) -> dict[str, Any]:
    """A fail-closed verdict carrying already-built flags (the named schema failures)."""
    import uuid

    result = assemble_and_report(
        "compiled-team",
        [],
        owner_organization_id=uuid.UUID(_UUID_NS),
        shape="compiled",
        extra_flags=flags,
    )
    return {
        "would_block": result.report.would_block,
        "blocking": result.report.blocking,
        "report": render_report(result.report),
    }


def validate_draft(
    draft: str | dict[str, Any],
    catalog: Any,
    *,
    owner_organization_id: Any,
    name: str = "compiled-team",
    substrate: Substrate = "graph",
) -> dict[str, Any]:
    """Diff a drafted Team Harness against the surveyed ``catalog`` + run the shared dry-run.

    Returns ``{"would_block": bool, "blocking": list[str], "report": str}`` — the reviewer ships the
    draft only when ``would_block`` is False; otherwise it re-drafts (bounded) with ``blocking``.

    ``substrate`` defaults to ``graph`` (ADR-040 Decision 7, cloud-first), where a file tool blocks
    with ``F-SUBSTRATE-FILE``. A default of ``file`` would put a tenant's deliverables in a
    server-side tmp tree, which is precisely what #694 reports."""
    data: Any
    if isinstance(draft, str):
        # a member's harness output is TEXT (#599): peel the JSON object out of the drafter LLM's
        # prose / ```json fence rather than json.loads the whole string (a real LLM never returns
        # bare JSON), so a valid draft wrapped in prose is not mis-blocked F-DRAFT-INVALID.
        match = re.search(r"\{.*\}", draft, re.DOTALL)
        if match is None:
            return _blocked("F-DRAFT-INVALID", "the draft has no JSON team manifest")
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return _blocked("F-DRAFT-INVALID", "the draft is not valid JSON")
    else:
        data = draft
    if not isinstance(data, dict) or not isinstance(data.get("members"), list):
        return _blocked("F-DRAFT-INVALID", "the draft is not an OHM team manifest with members[]")

    members: list[OHMMember] = []
    schema_flags: list[ImportFlag] = []
    for index, raw in enumerate(data["members"]):
        try:
            members.append(OHMMember.model_validate(raw))
        except ValidationError as exc:
            # #751: the generic sentence "a draft member failed schema validation" named no member
            # and no field, and REVIEWER_PROMPT tells the reviewer to edit exactly what the
            # blocking reasons name. Given nothing to edit it re-validated five times for a
            # byte-identical verdict and exhausted its cap at 24,050 tokens — while the Pydantic
            # error already said `members.4.depends_on.0: Input should be a valid string`.
            #
            # Collecting instead of returning on the first: a draft with two typos otherwise costs
            # two compiles against a reviewer allowed at most two fixes.
            schema_flags.append(
                ImportFlag(
                    code="F-DRAFT-INVALID",
                    severity="blocking",
                    member_role=_member_label(raw, index),
                    message=_schema_detail(raw, index, exc),
                )
            )
        except Exception:  # noqa: BLE001 — a non-pydantic surprise still fails closed, unnamed
            schema_flags.append(
                ImportFlag(
                    code="F-DRAFT-INVALID",
                    severity="blocking",
                    member_role=_member_label(raw, index),
                    message=(f"member {_member_label(raw, index)} failed schema validation"),
                )
            )
    if schema_flags:
        # A member that did not parse cannot be assembled, and assembling the survivors would
        # report a DAG built from half a team. Fail closed on the named reasons alone.
        return _blocked_with(schema_flags)

    # ADR-032 capability-absence: a tool not in the SURVEYED catalog is a blocking miss (the gate).
    allowed = _catalog_slugs(catalog)
    flags: list[ImportFlag] = []
    for m in members:
        for tool in m.tools:
            slug = _tool_slug(tool)
            # #694: the substrate reason takes PRECEDENCE over capability-absence for the same
            # tool. Both are true under the graph substrate — the catalog no longer lists ``write``
            # either — but the reviewer member is a MODEL that re-drafts against ``blocking``, and
            # "not in the catalog" sends it looking for a typo instead of a substrate. The member
            # role rides in the message because the rendered blocking line carries the code and the
            # message only.
            if substrate == "graph" and slug in _FILE_SUBSTRATE_TOOLS:
                flags.append(
                    ImportFlag(
                        code="F-SUBSTRATE-FILE",
                        severity="blocking",
                        member_role=m.role,
                        message=(
                            f"member {m.role!r} declares tool {tool!r}, which reads and writes a"
                            " per-organisation file sandbox that nothing else can see. Under the"
                            " graph substrate a member persists to the team's shared knowledge"
                            " graph: use 'graph-ingest' to write and 'knowledge-retriever' /"
                            " 'find-similar' to read"
                        ),
                    )
                )
                continue
            if not slug or slug not in allowed:  # an empty-slug tool ("@", "/") also fails closed
                flags.append(
                    ImportFlag(
                        code="F-CAPABILITY-MISSING",
                        severity="blocking",
                        member_role=m.role,
                        message=(
                            f"tool {tool!r} is not in the surveyed capability"
                            f" catalog.{_name_hint(slug, allowed)}"
                        ),
                    )
                )
            # #718: the gate cannot judge FIT, but it CAN require the drafter to have stated a
            # reason tied to THIS member's sub-goal — cheap to check, expensive to skip.
            if not m.tool_rationale.get(tool, "").strip():
                flags.append(
                    ImportFlag(
                        code="F-TOOL-UNJUSTIFIED",
                        severity="blocking",
                        member_role=m.role,
                        message=(
                            f"member {m.role!r} holds tool {tool!r} with no stated reason — add"
                            f' tool_rationale["{tool}"] explaining why THIS member needs it'
                        ),
                    )
                )

    # #697 (ruling 2026-08-24): every member declares what it hands on, with NO exception for a
    # member nobody depends on — the narrower rule would make adding a depends_on edge silently
    # change what an earlier member must produce. `validate_payload` has enforced a declared key
    # fail-closed since ADR-035; the contract was inert because nothing ever filled it (run
    # fe548aac: 14 members, all empty). The member ROLE rides in the message so the reviewer's
    # repair loop has something to edit — the lesson #751 pays for on the other field.
    for m in members:
        required = m.outputs_schema.get("required") if m.outputs_schema else None
        if isinstance(required, list) and required:
            continue
        flags.append(
            ImportFlag(
                code="F-NO-OUTPUT-CONTRACT",
                severity="blocking",
                member_role=m.role,
                message=(
                    f"member {m.role!r} declares no output keys. Every member states what it hands"
                    ' on, as outputs_schema {"required": ["<key>", …]} — the keys it will really'
                    " put in its answer, so the next member reads a named result instead of"
                    ' parsing an essay. Use ["summary"] when nothing more specific fits, and add'
                    ' "artifact_refs" when the member persists something'
                ),
            )
        )

    # #718 F-TEAM-NO-TOOLS (confirm, non-blocking): every member declared tools: [] while the
    # surveyed catalog offered something. Never drives would_block — worth a human's look, not a
    # block. An empty catalog means nothing was on offer, so an empty tools[] is not a signal.
    if allowed and members and all(not m.tools for m in members):
        flags.append(
            ImportFlag(
                code="F-TEAM-NO-TOOLS",
                severity="confirm",
                member_role="",
                message="every member of this team declares tools: [] — worth a human check",
            )
        )

    orchestration: OHMOrchestration | None = None
    raw_orch = data.get("orchestration")
    if isinstance(raw_orch, dict):
        try:
            orchestration = OHMOrchestration.model_validate(raw_orch)
        except Exception:  # noqa: BLE001 — a malformed orchestration blocks, never crashes
            flags.append(
                ImportFlag(
                    code="F-DRAFT-INVALID",
                    severity="blocking",
                    member_role="",
                    message="the draft orchestration failed schema validation",
                )
            )

    # #714 (Contract §TASK): the per-run task the team expects. A malformed block BLOCKS rather
    # than being quietly dropped — the reviewer's repair loop gets a named reason to fix it, and a
    # team that silently loses its task declaration is exactly the team #714 reports: one that
    # looks compiled and cannot be told which pull request to work on.
    raw_task_input = data.get("task_input")
    if raw_task_input is not None:
        try:
            OHMTaskInput.model_validate(raw_task_input)
        except Exception:  # noqa: BLE001 — a malformed task_input blocks, never crashes
            flags.append(
                ImportFlag(
                    code="F-DRAFT-INVALID",
                    severity="blocking",
                    member_role="",
                    message="the draft task_input failed schema validation",
                )
            )

    result = assemble_and_report(
        name,
        members,
        owner_organization_id=owner_organization_id,
        shape="compiled",
        orchestration=orchestration,
        extra_flags=flags,
    )
    return {
        "would_block": result.report.would_block,
        "blocking": result.report.blocking,
        "report": render_report(result.report),
    }
