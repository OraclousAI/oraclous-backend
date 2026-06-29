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


def test_prose_path_preserves_the_orchestrator_signal_for_o8() -> None:
    # regression guard: the orchestrator→prose delegation MERGES the orchestrator-path signal onto
    # the prose plan, so the O8 dry-run still sees the `--mode` surface. Reverting that merge to
    # `return plan` drops conditional_modes + these flags on the prose path (ADR-034 §7).
    plan = _plan()
    assert plan.conditional_modes == [
        "--full",
        "--graph",
    ]  # the subcommand modes survive delegation
    assert {
        "F-MODE-FULL",
        "F-MODE-GRAPH",
        "F-MEDIUM-INFERRED",
        "F-TERMINATION-ABSENT",
    } <= _codes(plan)


def test_import_setup_reaches_the_chapter_pipeline_end_to_end() -> None:
    # the production seam (a slice-1-style wiring guard): import_setup on a prose-coordinator dir
    # (no modules/) must REACH the prose adapter, not dead-end on F-NO-SETUP. The other tests call
    # adapt_orchestrator_skill directly, which would mask a missing import_setup hook.
    from oraclous_ohm.import_.setup import import_setup

    res = import_setup(_SKILLS / "book-studio", owner_organization_id=_ORG)
    assert res.report.shape == "orchestrator"  # detected as importable, not F-NO-SETUP
    assert res.manifest is not None
    roles = {m.role for m in res.manifest.members}
    assert _CHAPTER_AGENTS <= roles  # all 10+ chapter agents present
    assert {"gate-a", "gate-b", "gate-c"} <= roles  # the human gates wired in
    assert not res.report.blocking
    assert _CHAPTER_AGENTS <= set(
        res.sub_harnesses
    )  # runnable bodies came through (import->run seam)


def test_non_utf8_skill_md_fails_closed_not_crash(tmp_path: Path) -> None:
    # finding-1 regression: _is_orchestrator_dir now READS the SKILL.md for prose detection; a
    # non-UTF-8 file must degrade to "not an orchestrator dir" (F-NO-SETUP), NOT crash import_setup
    # with an uncaught UnicodeDecodeError (which is a UnicodeError, not an OSError).
    from oraclous_ohm.import_.setup import import_setup

    d = tmp_path / "weird-skill"
    d.mkdir()
    (d / "SKILL.md").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    res = import_setup(d, owner_organization_id=_ORG)  # must not raise
    assert res.manifest is None  # nothing importable — fail-closed, not a crash


def test_parser_skips_prose_lines_and_attaches_a_bare_gate(tmp_path: object) -> None:  # noqa: ARG001
    # review NIT-2 robustness: a numbered line with no `→` is prose (no junk member); a bare GATE
    # line attaches to the PREVIOUS stage instead of being dropped.
    from oraclous_ohm.import_.prose_coordinator import adapt_prose_coordinator_skill
    from oraclous_ohm.import_.skills import ResolvedSkill

    body = (
        "### `chapter <CH-NN>` — pipeline\n```\n"
        "1. alpha → does a thing\n"
        "2. the author reviews the work and decides\n"  # prose, no arrow → mints no member
        "3. ──▶ GATE A\n"  # a bare gate line → attaches to the previous stage (alpha)
        "4. beta → next\n```\n"
    )
    resolved = ResolvedSkill(name="x", kind="orchestrator", skill_name="x", body=body)
    plan = adapt_prose_coordinator_skill(resolved, owner_organization_id=_ORG, skills_root=_SKILLS)
    by = {m.role: m for m in plan.members}
    assert "the" not in by and "author" not in by  # the prose line minted no agent
    assert by["gate-a"].kind == "human" and by["gate-a"].depends_on == ["alpha"]  # bare gate → prev
    assert by["beta"].depends_on == ["gate-a"]  # the next step waits on the gate
