"""#1137 — the platform-owned save trigger (domain layer).

Validation Desk's decision brief was lost on 5 of 5 runs: the model must remember to call
``graph-ingest`` at the end of the run, and nothing enforces that it does. The engine already
holds everything it needs at settle to decide, on its own, whether a member's deliverable belongs
on the graph — so the trigger becomes a pure predicate over data the engine already has, never a
second trust in the model's tool-calling.

Ruled on #1137 (design comment, qa-engineer): a member is auto-saved at settle iff, ALL of —
  * the run is bound to a graph (``graph_id`` is not ``None``);
  * the member SETTLED with a non-null result — status ``succeeded`` or ``partial`` (#1015: a
    ``partial`` member, e.g. an ``empty_retrieval`` note, still carries a real answer; a member
    that never settled, or settled with nothing, has nothing to save);
  * its manifest DECLARED required output keys (``outputs_schema.required``, non-empty) — an
    undeclared member has no defined deliverable shape to save;
  * every declared key is present in the settled payload — a member that declared a key but did
    not deliver it has nothing there to write.

Fan-out members (a shared role across per-item sub-runs, #1015: no reliable per-item ordinal is
recoverable at settle) are OUT OF SCOPE for this first cut and are pinned skipped, not silently
passed through.

RED until ``domain/member_artifact.py`` and ``should_autosave`` exist; the seam is imported
function-locally per ``.claude/rules/tests-seam-imports.md``.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]

_DECLARED = ["posture", "headline"]
_PAYLOAD = {"posture": "prerequisite", "headline": "needs clearer marketing strategy"}


def test_a_graph_bound_succeeded_member_with_every_declared_key_is_autosaved() -> None:
    from oraclous_execution_engine_service.domain.member_artifact import should_autosave

    assert (
        should_autosave(
            graph_id="ad5bb59c-b24e-4fd8-ab80-013dc4838fff",
            status="succeeded",
            payload=_PAYLOAD,
            declared_keys=_DECLARED,
            is_fan_out=False,
        )
        is True
    )


def test_a_partial_settle_with_a_real_answer_is_still_autosaved() -> None:
    """#1015 live case: run 6332197e settled ``partial`` (``empty_retrieval``) with a genuine
    ``posture``/``headline`` answer. A ``partial`` status must not be treated as "nothing to
    save" — that is exactly the run this issue is about."""
    from oraclous_execution_engine_service.domain.member_artifact import should_autosave

    assert (
        should_autosave(
            graph_id="ad5bb59c-b24e-4fd8-ab80-013dc4838fff",
            status="partial",
            payload=_PAYLOAD,
            declared_keys=_DECLARED,
            is_fan_out=False,
        )
        is True
    )


def test_an_unbound_run_is_never_autosaved() -> None:
    """No graph, nowhere to write — even a perfect settle is a no-op."""
    from oraclous_execution_engine_service.domain.member_artifact import should_autosave

    assert (
        should_autosave(
            graph_id=None,
            status="succeeded",
            payload=_PAYLOAD,
            declared_keys=_DECLARED,
            is_fan_out=False,
        )
        is False
    )


def test_a_member_with_no_declared_output_keys_is_never_autosaved() -> None:
    """Every team compiled before #697 declares nothing — the platform save must not invent a
    deliverable shape for a member that never promised one."""
    from oraclous_execution_engine_service.domain.member_artifact import should_autosave

    assert (
        should_autosave(
            graph_id="ad5bb59c-b24e-4fd8-ab80-013dc4838fff",
            status="succeeded",
            payload={"output": "just prose"},
            declared_keys=[],
            is_fan_out=False,
        )
        is False
    )


def test_a_missing_declared_key_is_never_autosaved() -> None:
    """The member promised ``headline`` and did not deliver it — the platform never fabricates
    the missing piece, it simply does not save."""
    from oraclous_execution_engine_service.domain.member_artifact import should_autosave

    assert (
        should_autosave(
            graph_id="ad5bb59c-b24e-4fd8-ab80-013dc4838fff",
            status="succeeded",
            payload={"posture": "prerequisite"},
            declared_keys=_DECLARED,
            is_fan_out=False,
        )
        is False
    )


@pytest.mark.parametrize("status", ["failed", "blocked", "skipped", "budget_skipped", "running"])
def test_a_member_that_did_not_settle_succeeded_or_partial_is_never_autosaved(status: str) -> None:
    from oraclous_execution_engine_service.domain.member_artifact import should_autosave

    assert (
        should_autosave(
            graph_id="ad5bb59c-b24e-4fd8-ab80-013dc4838fff",
            status=status,
            payload=_PAYLOAD,
            declared_keys=_DECLARED,
            is_fan_out=False,
        )
        is False
    )


def test_a_fan_out_member_is_skipped_in_this_first_cut() -> None:
    """#1015: a fan-out member's sub-runs share ONE role, so the per-item ordinal that would
    disambiguate their artifacts is not recoverable at settle. Documented as out of scope, never
    silently treated as an ordinary single-dispatch member."""
    from oraclous_execution_engine_service.domain.member_artifact import should_autosave

    assert (
        should_autosave(
            graph_id="ad5bb59c-b24e-4fd8-ab80-013dc4838fff",
            status="succeeded",
            payload=_PAYLOAD,
            declared_keys=_DECLARED,
            is_fan_out=True,
        )
        is False
    )
