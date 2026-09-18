"""#1137 — the duplicate guard (domain layer).

One artifact listing for the run and member role decides whether the platform's save is
redundant. Content hashes cannot be the key: the model's own ``graph-ingest`` call writes the
model's prose, the platform writes canonical JSON of the declared keys, so a real prior save and
the platform's write never hash-match. Status is the only signal that generalises across both a
model that already saved successfully and an earlier platform write.

Ruled on #1137 (design comment, qa-engineer):
  * any returned row whose ``status`` is not ``"failed"`` suppresses the write — this covers both
    a model save that actually landed and the platform's own earlier attempt;
  * rows that are ALL ``"failed"`` do NOT suppress it — a failed attempt is not a save;
  * if the listing itself is inconclusive (the read errors), the platform writes anyway — ruled
    explicitly: a duplicate is recoverable, a lost deliverable is the defect being fixed. This
    pure function never talks to KGS itself — the service layer that DOES the read collapses a
    failed read to ``None`` before calling here, which is what lets this stay a pure predicate.

RED until ``domain/member_artifact.py`` and ``should_skip_as_duplicate`` exist; the seam is
imported function-locally per ``.claude/rules/tests-seam-imports.md``.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit]


def test_no_existing_rows_does_not_suppress_the_write() -> None:
    from oraclous_execution_engine_service.domain.member_artifact import should_skip_as_duplicate

    assert should_skip_as_duplicate([]) is False


def test_a_single_failed_row_does_not_suppress_the_write() -> None:
    from oraclous_execution_engine_service.domain.member_artifact import should_skip_as_duplicate

    assert should_skip_as_duplicate([{"status": "failed"}]) is False


def test_rows_that_are_all_failed_do_not_suppress_the_write() -> None:
    from oraclous_execution_engine_service.domain.member_artifact import should_skip_as_duplicate

    rows = [{"status": "failed"}, {"status": "failed"}, {"status": "failed"}]
    assert should_skip_as_duplicate(rows) is False


def test_a_completed_row_suppresses_the_write() -> None:
    """A model save that actually landed — the platform must not duplicate it."""
    from oraclous_execution_engine_service.domain.member_artifact import should_skip_as_duplicate

    assert should_skip_as_duplicate([{"status": "completed"}]) is True


def test_any_non_failed_row_among_failed_ones_suppresses_the_write() -> None:
    """A prior platform write (or a real model save) alongside unrelated failed attempts still
    counts as "already saved" — the presence of ONE good row is enough."""
    from oraclous_execution_engine_service.domain.member_artifact import should_skip_as_duplicate

    rows = [{"status": "failed"}, {"status": "completed"}, {"status": "failed"}]
    assert should_skip_as_duplicate(rows) is True


def test_a_pending_row_suppresses_the_write() -> None:
    """Not-yet-terminal is not "failed" either — an in-flight save still counts as claimed."""
    from oraclous_execution_engine_service.domain.member_artifact import should_skip_as_duplicate

    assert should_skip_as_duplicate([{"status": "pending"}]) is True


def test_an_inconclusive_listing_writes_anyway() -> None:
    """Ruled: if the listing itself errors, the platform writes anyway. A duplicate is
    recoverable; a lost deliverable is the defect. The service layer signals an inconclusive read
    as ``None``, never as ``[]`` (which means "listed successfully, found nothing")."""
    from oraclous_execution_engine_service.domain.member_artifact import should_skip_as_duplicate

    assert should_skip_as_duplicate(None) is False
