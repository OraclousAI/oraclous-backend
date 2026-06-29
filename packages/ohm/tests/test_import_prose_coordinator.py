"""#577 slice-2 — the book-studio prose coordinator's ``chapter`` pipeline parses to a team DAG.

The existing orchestrator adapter requires a ``modules/<wave>/`` layout; book-studio is prose with a
numbered ``chapter <CH-NN>`` pipeline and NO ``modules/`` dir, so today it dead-ends on
``F-ORCH-UNSTRUCTURED`` with zero members. slice-2 adds the prose-coordinator adapter: numbered
steps → agent members, sequence → ``depends_on``, ``∥`` → same-stage siblings, ``GATE A/B/C`` →
``kind:human`` barriers (``human_role="author"``), ``BLOCK on CRITICAL`` → a deferred flag.

Built against the REAL fixture at ``tests/fixtures/book-team``. RED until the [impl] adds
``adapt_prose_coordinator_skill`` + the ``orchestrator.py`` delegation when ``modules/`` is absent.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from oraclous_ohm.dag import topological_stages
from oraclous_ohm.import_.orchestrator import adapt_orchestrator_skill
from oraclous_ohm.import_.skills import resolve_skill

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")
_SKILLS = Path(__file__).parent / "fixtures" / "book-team" / ".claude" / "skills"

# the 10 numbered agents of the `chapter <CH-NN>` pipeline (fixture SKILL.md)
_CHAPTER_AGENTS = {
    "research-scout",
    "bible-keeper",
    "book-calibrate",
    "chapter-architect",
    "narrative-drafter",
    "developmental-editor",
    "fact-checker",
    "prose-lint",
    "book-integrity",
    "line-editor",
    "engagement-reviewer",
}


def _plan():  # noqa: ANN202
    resolved = resolve_skill("book-studio", _SKILLS)
    return adapt_orchestrator_skill(resolved, owner_organization_id=_ORG, skills_root=_SKILLS)


def _codes(plan) -> set[str]:  # noqa: ANN001
    return {f.code for f in plan.flags}


def test_prose_adapter_triggers_instead_of_unstructured() -> None:
    plan = _plan()
    assert "F-ORCH-UNSTRUCTURED" not in _codes(
        plan
    )  # the prose adapter handles the no-modules case
    assert "F-PROSE-CHAPTER" in _codes(plan)  # it parsed the chapter pipeline


def test_chapter_pipeline_yields_the_agents_and_the_author_gates() -> None:
    by = {m.role: m for m in _plan().members}
    assert _CHAPTER_AGENTS <= set(by)  # the 10 numbered agents become members
    for gate in ("gate-a", "gate-b", "gate-c"):
        assert by[gate].kind == "human" and by[gate].human_role == "author"


def test_pipeline_depends_on_threads_through_the_gates() -> None:
    by = {m.role: m for m in _plan().members}
    assert by["research-scout"].depends_on == []  # step 1 — the head
    assert by["bible-keeper"].depends_on == ["research-scout"]  # 2 ← 1
    assert by["gate-a"].depends_on == ["book-calibrate"]  # GATE A after step 3
    assert by["chapter-architect"].depends_on == ["gate-a"]  # step 4 waits on the gate (a barrier)
    # step 7: fact-checker ∥ prose-lint — both gated by GATE B
    assert by["fact-checker"].depends_on == ["gate-b"]
    assert by["prose-lint"].depends_on == ["gate-b"]
    assert set(by["book-integrity"].depends_on) == {"fact-checker", "prose-lint"}  # 8 ← the ∥ pair
    assert by["gate-c"].depends_on == ["engagement-reviewer"]  # GATE C after step 10


def test_parallel_pair_shares_one_topological_stage() -> None:
    stages = topological_stages(_plan().members)
    stage_of = {role: i for i, s in enumerate(stages) for role in s}
    assert stage_of["fact-checker"] == stage_of["prose-lint"]  # ∥ → the same stage


def test_block_on_critical_is_flagged_and_deferred() -> None:
    # step 8 `book-integrity … BLOCK on CRITICAL` is surfaced; the runtime skip-guard is deferred.
    assert "F-PROSE-BLOCK" in _codes(_plan())
