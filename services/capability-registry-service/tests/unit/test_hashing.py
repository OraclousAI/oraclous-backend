"""Unit: content hashing is canonical (order-independent, None-stripped, stable)."""

from __future__ import annotations

import pytest
from oraclous_capability_registry_service.domain.hashing import compute_content_hash

pytestmark = [pytest.mark.unit, pytest.mark.capability_integrity]


def test_key_order_is_irrelevant() -> None:
    a = {"b": 1, "a": {"y": 2, "x": 3}}
    b = {"a": {"x": 3, "y": 2}, "b": 1}
    assert compute_content_hash(a) == compute_content_hash(b)


def test_none_values_are_stripped() -> None:
    with_none = {"a": 1, "b": None, "c": {"d": None, "e": 2}}
    without = {"a": 1, "c": {"e": 2}}
    assert compute_content_hash(with_none) == compute_content_hash(without)


def test_distinct_payloads_hash_differently() -> None:
    assert compute_content_hash({"a": 1}) != compute_content_hash({"a": 2})


def test_hash_is_sha256_hex() -> None:
    digest = compute_content_hash({"a": 1})
    assert len(digest) == 64
    int(digest, 16)  # raises if not hex
