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

import json
import re
from typing import Any

from oraclous_ohm.import_ import ImportFlag, assemble_and_report, render_report
from oraclous_ohm.manifest import OHMMember, OHMOrchestration

_UUID_NS = "00000000-0000-0000-0000-000000000000"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def _catalog_slugs(catalog: Any) -> set[str]:
    """The set of SURVEYED tool identifiers (slugged) the drafter is allowed to draw from. Accepts a
    list of bare names/refs OR of dicts ({name|binding|ref}) — whatever the survey tool returned."""
    out: set[str] = set()
    items = catalog.get("tools", catalog) if isinstance(catalog, dict) else catalog
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            out.add(_slug(it))
        elif isinstance(it, dict):
            for key in ("binding", "name", "ref"):
                val = it.get(key)
                if isinstance(val, str) and val:
                    # a ref like "org:x/web-research@1" → its tail slug "web-research"
                    out.add(_slug(val.split("/")[-1].split("@")[0]))
    return out


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


def validate_draft(
    draft: str | dict[str, Any],
    catalog: Any,
    *,
    owner_organization_id: Any,
    name: str = "compiled-team",
) -> dict[str, Any]:
    """Diff a drafted Team Harness against the surveyed ``catalog`` + run the shared dry-run.

    Returns ``{"would_block": bool, "blocking": list[str], "report": str}`` — the reviewer ships the
    draft only when ``would_block`` is False; otherwise it re-drafts (bounded) with ``blocking``."""
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
    for raw in data["members"]:
        try:
            members.append(OHMMember.model_validate(raw))
        except Exception:  # noqa: BLE001 — a malformed member is a draft defect, fail-closed
            return _blocked("F-DRAFT-INVALID", "a draft member failed schema validation")

    # ADR-032 capability-absence: a tool not in the SURVEYED catalog is a blocking miss (the gate).
    allowed = _catalog_slugs(catalog)
    flags: list[ImportFlag] = []
    for m in members:
        for tool in m.tools:
            if _slug(tool) not in allowed:
                flags.append(
                    ImportFlag(
                        code="F-CAPABILITY-MISSING",
                        severity="blocking",
                        member_role=m.role,
                        message=f"tool {tool!r} is not in the surveyed capability catalog",
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
