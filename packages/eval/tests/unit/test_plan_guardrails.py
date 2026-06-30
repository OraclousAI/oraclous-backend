"""#597 (ADR-047 §7) — Layer-1 deterministic plan guardrails.

A known-good generated manifest PASSES (GO: ready); every adversarial manifest — cycle, duplicate
role, unknown depends_on, undeclared tool, member-cap>pool, missing capability, human-without-role,
and a ceiling-widening sub-harness — FAILS CLOSED (GO: BLOCKED), reusing the importer's validator.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_eval.guardrails import GuardrailReport, run_plan_guardrails

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_CATALOG = ["web-research", "write", "edit"]


def _member(role: str, deps: list[str], **kw: object) -> dict[str, object]:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"org:compiled/{role}@1",
        "depends_on": deps,
        **kw,
    }


def _draft(members: list[dict[str, object]], **top: object) -> dict[str, object]:
    return {
        "members": members,
        "orchestration": {"style": "linear", "success_criteria": "done"},
        **top,
    }


_GOOD = _draft(
    [
        _member("researcher", [], tools=["web-research"]),
        _member("writer", ["researcher"], tools=["write"]),
    ],
    budget={"max_tokens_total": 500_000, "max_tokens_per_member": 100_000},
)


def _run(draft: dict[str, object], **kw: object) -> GuardrailReport:
    return run_plan_guardrails(draft, owner_organization_id=_ORG, catalog=_CATALOG, **kw)


def test_a_known_good_manifest_passes_go_ready() -> None:
    report = _run(_GOOD)
    assert report.passed is True
    assert report.would_block is False
    assert report.blocking == []
    assert "GO: ready" in report.render()
    # every guardrail dimension ran and passed
    by = {c.name: c for c in report.checks}
    assert (
        by["dag_acyclic"].passed
        and by["capability_absence"].passed
        and by["cap_within_pool"].passed
    )


def test_a_dependency_cycle_blocks() -> None:
    # the assembler DEMOTES a cycle to a loop seam — Layer-1's topological_stages catches it.
    report = _run(_draft([_member("a", ["b"]), _member("b", ["a"])]))
    assert report.would_block is True
    assert any("F-DAG-INVALID" in b and "cycle" in b for b in report.blocking)
    assert "GO: BLOCKED" in report.render()


def test_a_duplicate_role_blocks() -> None:
    report = _run(_draft([_member("a", []), _member("a", [])]))
    assert report.would_block is True
    assert any("duplicate member role" in b for b in report.blocking)


def test_an_unknown_depends_on_blocks() -> None:
    report = _run(_draft([_member("a", ["ghost"])]))
    assert report.would_block is True
    assert any("unknown member" in b for b in report.blocking)


def test_an_undeclared_tool_blocks_capability_absence() -> None:
    report = _run(_draft([_member("a", [], tools=["evil-tool"])]))
    assert report.would_block is True
    assert any("F-CAPABILITY-MISSING" in b for b in report.blocking)


def test_a_foreign_namespace_tool_cannot_masquerade_as_surveyed() -> None:
    # #594 hardening surfaces through Layer-1: evil/web-research must not pass as web-research.
    report = _run(_draft([_member("a", [], tools=["evil/web-research"])]))
    assert report.would_block is True
    assert any("F-CAPABILITY-MISSING" in b for b in report.blocking)


def test_a_member_cap_above_the_pool_blocks() -> None:
    draft = _draft(
        [_member("greedy", [], tools=["write"], max_tokens=9_000_000)],
        budget={"max_tokens_total": 1_000_000},
    )
    report = _run(draft)
    assert report.would_block is True
    assert any("F-CAP-OVER-POOL" in b and "max_tokens" in b for b in report.blocking)


def test_a_member_tool_call_cap_above_the_pool_blocks() -> None:
    draft = _draft(
        [_member("greedy", [], tools=["write"], max_tool_calls=500)],
        budget={"max_tool_calls_total": 50},
    )
    report = _run(draft)
    assert report.would_block is True
    assert any("F-CAP-OVER-POOL" in b and "max_tool_calls" in b for b in report.blocking)


def test_a_member_cap_equal_to_the_pool_is_allowed() -> None:
    draft = _draft(
        [_member("ok", [], tools=["write"], max_tokens=1_000_000)],
        budget={"max_tokens_total": 1_000_000},
    )
    report = _run(draft)
    assert report.passed is True


def test_a_human_member_without_a_role_blocks_schema() -> None:
    report = _run(_draft([_member("a", []), {"role": "h", "kind": "human", "depends_on": ["a"]}]))
    assert report.would_block is True
    assert any("schema" in b.lower() or "F-DRAFT-INVALID" in b for b in report.blocking)


def test_a_non_manifest_draft_fails_closed() -> None:
    report = run_plan_guardrails(
        "not a manifest at all", owner_organization_id=_ORG, catalog=_CATALOG
    )
    assert report.would_block is True
    assert "GO: BLOCKED" in report.render()


def test_a_draft_as_prose_with_embedded_json_is_parsed() -> None:
    prose = "Here is the team:\n```json\n" + __import__("json").dumps(_GOOD) + "\n```\nDone."
    report = _run(prose)
    assert report.passed is True


def test_a_ceiling_widening_sub_harness_blocks() -> None:
    # ADR-032: a member with tools=[write] whose sub-harness declares capabilities=[write, bash]
    # widens its ceiling — a blocking violation.
    draft = _draft([_member("w", [], tools=["write"])])
    sub = {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "w-sub",
            "owner_organization_id": str(_ORG),
        },
        "capabilities": [
            {"ref": "core/write@1", "binding": "write"},
            {"ref": "core/bash@1", "binding": "bash"},  # widens the member's {write} ceiling
        ],
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }
    report = _run(draft, sub_harnesses={"w": sub})
    assert report.would_block is True
    assert any("F-CEILING-EXCEEDED" in b for b in report.blocking)
