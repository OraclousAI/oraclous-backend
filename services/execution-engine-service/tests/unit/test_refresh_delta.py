"""#602 — the pure seeded-refresh 5-way classifier (domain/refresh.py). Deterministic, no I/O."""

from __future__ import annotations

import pytest
from oraclous_execution_engine_service.domain.refresh import (
    REFRESH_STATUS_FIELD,
    compute_delta,
    parse_records,
)

pytestmark = pytest.mark.unit


def _rec(rid: str, val: str, *, skip: bool = False) -> dict:
    r = {"id": rid, "value": val}
    if skip:
        r[REFRESH_STATUS_FIELD] = "unchanged"
    return r


# ── parse_records ───────────────────────────────────────────────────────────────────────────────
def test_parse_records_reads_a_json_array_of_objects() -> None:
    assert parse_records('[{"id": "a"}, {"id": "b"}]') == [{"id": "a"}, {"id": "b"}]


def test_parse_records_accepts_an_already_parsed_list() -> None:
    assert parse_records([{"id": "a"}]) == [{"id": "a"}]


def test_parse_records_returns_none_for_a_non_list_deliverable() -> None:
    # a scalar / prose / object deliverable has no per-record delta — None, not a false empty delta
    assert parse_records("just prose, not a ledger") is None
    assert parse_records('{"id": "a"}') is None
    assert parse_records("42") is None


def test_parse_records_drops_non_dict_rows() -> None:
    assert parse_records('[{"id": "a"}, 7, "x"]') == [{"id": "a"}]


def test_parse_records_unwraps_a_whole_string_json_fence() -> None:
    assert parse_records('```json\n[{"id": "a"}]\n```') == [{"id": "a"}]


def test_parse_records_extracts_a_json_fence_embedded_after_prose_reasoning() -> None:
    # #602: a cold run reasons in prose, THEN emits the ledger in a fence — extract the fenced
    # array (the whole deliverable is not itself JSON). This is what lets derive-then-emit work.
    out = parse_records(
        "Let me think about each item.\n"
        "Item a: still valid. Item b: still valid.\n\n"
        '```json\n[{"id": "a", "v": 1}, {"id": "b", "v": 2}]\n```\n\nDone.'
    )
    assert out == [{"id": "a", "v": 1}, {"id": "b", "v": 2}]


def test_parse_records_takes_the_last_fenced_record_array_the_ledger_at_the_end() -> None:
    # #602 review Finding 1: the ledger is emitted "at the very end", after any illustrative example
    # fence — the LAST record array wins, so an earlier example never smuggles a wrong delta.
    out = parse_records(
        'For example ```json\n[{"id": "EXAMPLE"}]\n``` — now my real output:\n'
        '```json\n[{"id": "alpha"}, {"id": "bravo"}]\n```'
    )
    assert out == [{"id": "alpha"}, {"id": "bravo"}]  # the final ledger, not the EXAMPLE


def test_parse_records_skips_non_array_fences_and_takes_the_last_record_array() -> None:
    out = parse_records('```json\n{"not": "an array"}\n``` then ```\n[{"id": "z"}]\n```')
    assert out == [{"id": "z"}]  # the non-array block is skipped; the record array wins


def test_parse_records_returns_none_when_no_fenced_or_whole_json_array() -> None:
    assert parse_records("prose only, no json here at all") is None
    assert parse_records('```json\n{"id": "a"}\n```') is None  # a fenced OBJECT is not a record-set


# ── the 5-way classification ──────────────────────────────────────────────────────────────────────
def test_added_removed_changed_are_deterministic_from_fingerprints() -> None:
    seed = [_rec("1", "x"), _rec("2", "y"), _rec("3", "z")]
    fresh = [
        _rec("1", "x", skip=True),
        _rec("2", "Y-CHANGED"),
        _rec("4", "new"),
    ]  # 3 removed, 4 added
    d = compute_delta(seed, fresh)
    assert d["counts"] == {
        "added": 1,
        "removed": 1,
        "changed": 1,
        "unchanged": 1,
        "re_confirmed": 0,
    }
    assert d["added"][0]["id"] == "4"
    assert d["removed"][0]["id"] == "3"
    assert d["changed"][0]["id"] == "2"
    assert d["unchanged"][0]["id"] == "1"
    assert d["skipped"] == 1  # the cost-saving signal = the unchanged count


def test_unchanged_requires_an_explicit_skip_marker_fail_open_to_re_confirmed() -> None:
    # a fingerprint MATCH WITHOUT a skip marker is re_confirmed (re-examined), not a false unchanged
    seed = [_rec("1", "x"), _rec("2", "y")]
    fresh = [_rec("1", "x"), _rec("2", "y", skip=True)]  # #1 no marker, #2 marked skipped
    d = compute_delta(seed, fresh)
    assert d["counts"]["unchanged"] == 1 and d["unchanged"][0]["id"] == "2"
    assert d["counts"]["re_confirmed"] == 1 and d["re_confirmed"][0]["id"] == "1"
    assert d["counts"]["changed"] == 0  # identical fingerprint → not changed


def test_re_confirmed_is_distinct_from_unchanged_lock_o3() -> None:
    # the Lock O3 guarantee: a re-examined-still-true record must NOT be reported as unchanged
    seed = [_rec("1", "x")]
    fresh = [_rec("1", "x")]  # same value, re-examined, no skip marker
    d = compute_delta(seed, fresh)
    assert d["re_confirmed"] and not d["unchanged"]


def test_a_skip_marker_never_overrides_a_real_change() -> None:
    # even if a member lies "unchanged" on a record whose evidence moved, the fingerprint wins
    seed = [_rec("1", "x")]
    fresh = [{"id": "1", "value": "MOVED", REFRESH_STATUS_FIELD: "unchanged"}]
    d = compute_delta(seed, fresh)
    assert d["counts"]["changed"] == 1 and d["counts"]["unchanged"] == 0


def test_the_skip_marker_is_not_part_of_the_evidence_fingerprint() -> None:
    # adding/removing the transport-only marker must not read as a change
    seed = [{"id": "1", "value": "x"}]
    fresh = [{"id": "1", "value": "x", REFRESH_STATUS_FIELD: "unchanged"}]
    d = compute_delta(seed, fresh)
    assert d["counts"]["changed"] == 0 and d["counts"]["unchanged"] == 1


def test_no_id_field_falls_back_to_content_identity_never_a_false_unchanged() -> None:
    # records without the id field are identified by content; a value edit reads as removed+added
    seed = [{"value": "x"}]
    fresh = [{"value": "MOVED", REFRESH_STATUS_FIELD: "unchanged"}]
    d = compute_delta(seed, fresh)
    assert d["counts"]["added"] == 1 and d["counts"]["removed"] == 1
    assert d["counts"]["unchanged"] == 0  # a content-identified record can never falsely skip


def test_empty_seed_classifies_everything_added() -> None:
    d = compute_delta([], [_rec("1", "x"), _rec("2", "y")])
    assert d["counts"]["added"] == 2 and d["counts"]["removed"] == 0


def test_empty_fresh_classifies_everything_removed() -> None:
    d = compute_delta([_rec("1", "x")], [])
    assert d["counts"]["removed"] == 1 and d["counts"]["added"] == 0


def test_a_custom_id_field_keys_the_records() -> None:
    seed = [{"route": "PAR-LON", "price": 10}]
    fresh = [{"route": "PAR-LON", "price": 12}]  # same route id, changed price
    d = compute_delta(seed, fresh, id_field="route")
    assert d["counts"]["changed"] == 1 and d["id_field"] == "route"


def test_parse_records_unwraps_a_markdown_json_fence() -> None:
    # LLM members often fence their JSON deliverable — the ledger must still parse
    fenced = '```json\n[{"id": "1"}, {"id": "2"}]\n```'
    assert parse_records(fenced) == [{"id": "1"}, {"id": "2"}]
    assert parse_records('```\n[{"id": "a"}]\n```') == [{"id": "a"}]


# ── review fixes: the fingerprint/identity soundness holes (Lock O3 — never silently worse) ───────
def test_a_signatures_field_change_is_changed_not_a_false_unchanged() -> None:
    # REFRESH-02 (HIGH): OHM's content_hash strips a top-level ``signatures`` key; a record field
    # named ``signatures`` MUST still be part of the evidence fingerprint, else a change confined to
    # it reads as a false unchanged (the local _record_hash excludes ONLY refresh_status).
    seed = [{"id": "a", "value": "x", "signatures": "A"}]
    fresh = [{"id": "a", "value": "x", "signatures": "B", REFRESH_STATUS_FIELD: "unchanged"}]
    d = compute_delta(seed, fresh)
    assert d["counts"]["changed"] == 1 and d["counts"]["unchanged"] == 0, d
    assert {r["id"] for r in d["changed"]} == {"a"}


def test_a_duplicate_fresh_id_fails_closed_to_changed_never_a_skip() -> None:
    # REFRESH-01 (MED): a member emitting the same id twice (a carried skip-copy + a re-derived one)
    # must NEVER be credited unchanged — the moved copy could be smuggled through as a skip. Fail
    # CLOSED to changed + surface the duplicate.
    seed = [{"id": "1", "value": "x"}]
    fresh = [
        {"id": "1", "value": "x", REFRESH_STATUS_FIELD: "unchanged"},  # skip-copy first
        {"id": "1", "value": "MOVED"},  # the real change, previously dropped
    ]
    d = compute_delta(seed, fresh)
    assert d["counts"]["unchanged"] == 0, d  # never a false skip
    assert d["counts"]["changed"] == 1 and d["duplicate_fresh_ids"] == 1, d


def test_distinct_typed_ids_do_not_collide() -> None:
    # REFRESH-03 (LOW): a str "1" and an int 1 are DISTINCT records — an un-typed key would collide
    # them and silently drop one. Both must be classified (here: both added against an empty seed).
    d = compute_delta([], [{"id": "1", "value": "s"}, {"id": 1, "value": "i"}])
    assert d["counts"]["added"] == 2, d
    assert d["duplicate_fresh_ids"] == 0, d
