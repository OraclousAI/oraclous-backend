"""Unit tests for the community domain layer (#303): deterministic id hashing + parent links.

Pure functions — no Neo4j, no LLM. The id-hashing test pins the legacy 16-char SHA-256 contract so
a re-detection of the same membership is idempotent (same id) and a different membership is a
different node; the parent-link test pins the majority-vote hierarchy that reproduces
PARENT_COMMUNITY.
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.domain.community import (
    DEFAULT_LEVELS,
    DEFAULT_RESOLUTIONS,
    build_parent_links,
    make_community_id,
)

pytestmark = pytest.mark.unit


def test_resolution_sweep_is_five_levels_ascending() -> None:
    # The 5-level hierarchy contract: 5 resolutions, ascending (coarse → fine), levels 0..4.
    assert len(DEFAULT_RESOLUTIONS) == 5
    assert list(DEFAULT_RESOLUTIONS) == sorted(DEFAULT_RESOLUTIONS)
    assert DEFAULT_LEVELS == (0, 1, 2, 3, 4)


def test_community_id_is_deterministic_and_order_independent() -> None:
    a = make_community_id(graph_id="g", level=1, resolution=2.0, member_ids=["e3", "e1", "e2"])
    b = make_community_id(graph_id="g", level=1, resolution=2.0, member_ids=["e1", "e2", "e3"])
    # Same members in any order → same id (sorted before hashing).
    assert a == b
    # Legacy scheme: community_<16 hex chars>.
    assert a.startswith("community_")
    assert len(a) == len("community_") + 16


def test_community_id_varies_with_membership_level_and_graph() -> None:
    base = make_community_id(graph_id="g", level=1, resolution=2.0, member_ids=["e1", "e2"])
    assert base != make_community_id(
        graph_id="g", level=1, resolution=2.0, member_ids=["e1", "e2", "e3"]
    )  # different members
    assert base != make_community_id(
        graph_id="g", level=2, resolution=2.0, member_ids=["e1", "e2"]
    )  # different level
    assert base != make_community_id(
        graph_id="other", level=1, resolution=2.0, member_ids=["e1", "e2"]
    )  # different graph


def test_build_parent_links_majority_vote() -> None:
    # Level 0 (coarse): one community of all four entities. Level 1 (fine): two sub-communities.
    membership = {
        0: {"c0": ["e1", "e2", "e3", "e4"]},
        1: {"cA": ["e1", "e2"], "cB": ["e3", "e4"]},
    }
    parents = build_parent_links(membership)
    # Coarsest level has no parents.
    assert parents[0]["c0"] is None
    # Both fine communities' members all live in c0 → c0 is their parent.
    assert parents[1]["cA"] == "c0"
    assert parents[1]["cB"] == "c0"


def test_build_parent_links_splits_to_distinct_parents() -> None:
    membership = {
        0: {"p1": ["e1", "e2"], "p2": ["e3", "e4"]},
        1: {"cA": ["e1", "e2"], "cB": ["e3"], "cC": ["e4"]},
    }
    parents = build_parent_links(membership)
    assert parents[1]["cA"] == "p1"  # both members under p1
    assert parents[1]["cB"] == "p2"  # e3 under p2
    assert parents[1]["cC"] == "p2"  # e4 under p2
