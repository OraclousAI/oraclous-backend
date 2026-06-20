"""The deterministic merge reducer (#421; ADR-035 §2/B3).

Replaces the round-table's last-writer-wins with concat / dedupe / group_by over fanned-out outputs.
"""

from __future__ import annotations

import pytest
from oraclous_ohm.aggregate import aggregate_reduce
from oraclous_ohm.errors import OHMError


def test_concat_field_lists() -> None:
    # EURail shape: each batch is a dict with an 'evidence' list -> one merged ledger
    outputs = [{"evidence": [1, 2]}, {"evidence": [3]}, {"evidence": [4, 5]}]
    assert aggregate_reduce(outputs, strategy="concat", field="evidence") == [1, 2, 3, 4, 5]


def test_concat_plain_lists() -> None:
    assert aggregate_reduce([[1, 2], [3], [4]], strategy="concat") == [1, 2, 3, 4]


def test_dedupe_by_value() -> None:
    outputs = [{"e": [1, 2]}, {"e": [2, 3]}, {"e": [3, 4]}]
    assert aggregate_reduce(outputs, strategy="dedupe", field="e") == [1, 2, 3, 4]


def test_dedupe_on_key_first_wins() -> None:
    items = [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 1, "v": "c"}]
    result = aggregate_reduce([items], strategy="dedupe", on="id")
    assert result == [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]  # the id=1 duplicate dropped


def test_group_by_key() -> None:
    items = [{"t": "x", "n": 1}, {"t": "y", "n": 2}, {"t": "x", "n": 3}]
    groups = aggregate_reduce([items], strategy="group_by", key="t")
    assert set(groups) == {"x", "y"}
    assert [i["n"] for i in groups["x"]] == [1, 3]


def test_group_by_requires_key() -> None:
    with pytest.raises(OHMError):
        aggregate_reduce([[{"t": "x"}]], strategy="group_by")


def test_unknown_strategy_fails_closed() -> None:
    with pytest.raises(OHMError):
        aggregate_reduce([[1]], strategy="nonsense")
