"""OHM canonical serialisation + content hash (slice 2): deterministic, signature-excluded."""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.ohm.canonical import canonical_bytes, content_hash

pytestmark = pytest.mark.unit

_DOC = {
    "ohm_version": "1.0",
    "metadata": {"name": "X", "id": "01976e3a-7c9b-7b00-9c45-1234567890ab"},
    "runtime": {"entrypoint": "pg"},
}


def test_hash_is_stable_regardless_of_key_order() -> None:
    a = dict(_DOC)
    b = {"runtime": {"entrypoint": "pg"}, "ohm_version": "1.0", "metadata": _DOC["metadata"]}
    assert content_hash(a) == content_hash(b)


def test_hash_excludes_signatures() -> None:
    unsigned = content_hash(_DOC)
    signed = content_hash({**_DOC, "signatures": [{"signer": "k", "signature": "zzz"}]})
    assert unsigned == signed  # adding/removing signatures must not change the artifact identity


def test_hash_changes_when_content_changes() -> None:
    other = {**_DOC, "metadata": {**_DOC["metadata"], "name": "Y"}}
    assert content_hash(_DOC) != content_hash(other)


def test_hash_is_64_hex_chars() -> None:
    h = content_hash(_DOC)
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_canonical_bytes_are_compact_sorted_json() -> None:
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
