"""Entity-resolution domain (#279) — the pure candidate-id + pair invariants, no I/O."""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.domain.resolution import (
    CandidatePair,
    candidate_id,
)

pytestmark = pytest.mark.unit


def test_candidate_id_is_order_independent() -> None:
    # The same pair yields the same id regardless of endpoint order (SAME_AS_CANDIDATE is undirected
    # for review) — so two reviewers, or the two URL orderings, key the same candidate.
    assert candidate_id("aaa", "bbb") == candidate_id("bbb", "aaa")


def test_candidate_id_is_stable_and_opaque() -> None:
    cid = candidate_id("node-1", "node-2")
    assert cid == candidate_id("node-1", "node-2")  # reproducible (no server state)
    assert len(cid) == 64 and cid.isalnum()  # sha256 hex; leaks neither node id
    assert "node-1" not in cid and "node-2" not in cid


def test_distinct_pairs_have_distinct_ids() -> None:
    assert candidate_id("a", "b") != candidate_id("a", "c")


def test_pair_exposes_its_canonical_id() -> None:
    pair = CandidatePair(node_id_a="x", node_id_b="y")
    assert pair.candidate_id == candidate_id("x", "y")
    assert pair.candidate_id == CandidatePair(node_id_a="y", node_id_b="x").candidate_id


def test_pair_rejects_identical_nodes() -> None:
    with pytest.raises(ValueError, match="distinct"):
        CandidatePair(node_id_a="same", node_id_b="same")


@pytest.mark.parametrize("a,b", [("", "y"), ("x", ""), (" ", "y")])
def test_pair_rejects_empty_node_id(a: str, b: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        CandidatePair(node_id_a=a, node_id_b=b)
