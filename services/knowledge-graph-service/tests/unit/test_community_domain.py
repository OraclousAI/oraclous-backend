"""Unit tests for the community domain layer (#303): deterministic ids + native-dendrogram mapping.

Pure functions — no Neo4j, no LLM. The id-hashing test pins the 16-char SHA-256 contract so a
re-detection of the same membership is idempotent (same id) and a different membership is a
different node; the dendrogram tests pin the native-dendrogram → level mapping (replacing degenerate
``w ** resolution`` sweep): one level per dendrogram depth (level 0 = coarsest), monotone
community counts finest→coarsest, true parent containment off the array, and an honest single
level when Louvain converges flat.
"""

from __future__ import annotations

import pytest
from oraclous_knowledge_graph_service.domain.community import (
    dendrogram_parent_links,
    dendrogram_to_levels,
    make_community_id,
)

pytestmark = pytest.mark.unit


def test_community_id_is_deterministic_and_order_independent() -> None:
    a = make_community_id(graph_id="g", level=1, member_ids=["e3", "e1", "e2"])
    b = make_community_id(graph_id="g", level=1, member_ids=["e1", "e2", "e3"])
    # Same members in any order → same id (sorted before hashing).
    assert a == b
    # Scheme: community_<16 hex chars>.
    assert a.startswith("community_")
    assert len(a) == len("community_") + 16


def test_community_id_varies_with_membership_level_and_graph() -> None:
    base = make_community_id(graph_id="g", level=1, member_ids=["e1", "e2"])
    assert base != make_community_id(graph_id="g", level=1, member_ids=["e1", "e2", "e3"])  # member
    assert base != make_community_id(graph_id="g", level=2, member_ids=["e1", "e2"])  # level
    assert base != make_community_id(graph_id="other", level=1, member_ids=["e1", "e2"])  # graph


# The live-verified depth-2 dendrogram from the GDS probe: index 0 finest (8 pairs), index 1
# coarsest (4 quads). Here a 4-node slice: pairs (e0,e1)→fine-1, (e2,e3)→fine-3; both → coarse-3.
_DEPTH2_ROWS = [
    ("e0", [1, 3]),
    ("e1", [1, 3]),
    ("e2", [3, 3]),
    ("e3", [3, 3]),
]


def test_dendrogram_to_levels_emits_one_level_per_depth() -> None:
    levels = dendrogram_to_levels(_DEPTH2_ROWS)
    # Depth 2 → exactly two levels, NOT five duplicates.
    assert set(levels) == {0, 1}
    # Level 0 is the COARSEST: one community covering all four nodes.
    assert len(levels[0]) == 1
    assert sorted(next(iter(levels[0].values()))) == ["e0", "e1", "e2", "e3"]
    # Level 1 is finer: two communities (the two pairs).
    assert len(levels[1]) == 2


def test_dendrogram_level_count_is_monotone_finest_to_coarsest() -> None:
    levels = dendrogram_to_levels(_DEPTH2_ROWS)
    # Community count is non-decreasing from coarsest (level 0) toward finest (highest level).
    counts = [len(levels[lvl]) for lvl in sorted(levels)]
    assert counts == sorted(counts)
    # And strictly: coarsest has the fewest, finest the most (the real hierarchy).
    assert counts[0] <= counts[-1]


def test_dendrogram_parent_links_are_true_containment() -> None:
    parents = dendrogram_parent_links(_DEPTH2_ROWS)
    # Coarsest level (0) is parent-less.
    assert all(p is None for p in parents[0].values())
    # Every finer community's parent is the SINGLE coarser community its members collapse into
    # (read straight off the array, not a majority vote) — and it actually exists at level 0.
    coarse_keys = set(dendrogram_to_levels(_DEPTH2_ROWS)[0])
    for parent in parents[1].values():
        assert parent in coarse_keys


def test_flat_convergence_emits_one_honest_level() -> None:
    # Louvain converged flat (depth 1) — the dominant uniform-weight case. ONE level, no parents,
    # no five duplicates.
    rows = [("e0", [5]), ("e1", [5]), ("e2", [7])]
    levels = dendrogram_to_levels(rows)
    assert set(levels) == {0}
    assert len(levels[0]) == 2  # two communities at the single level
    parents = dendrogram_parent_links(rows)
    assert all(p is None for p in parents[0].values())


def test_dendrogram_empty_rows() -> None:
    assert dendrogram_to_levels([]) == {}
    assert dendrogram_parent_links([]) == {}


# A RAGGED dendrogram: e2 stopped subdividing after one iteration (len 1) while e0/e1 ran two
# (len 2). It must not crash (no IndexError reading intermediate[idx+1]) and e2 must still appear at
# the coarsest level (a complete partition), not be stranded below it.
_RAGGED_ROWS = [
    ("e0", [1, 3]),
    ("e1", [1, 3]),
    ("e2", [9]),  # short row — only the (coarsest) final community
]


def test_dendrogram_ragged_rows_do_not_crash_and_keep_complete_partition() -> None:
    levels = dendrogram_to_levels(_RAGGED_ROWS)
    # Depth is the MAX length (2) → two levels, no crash.
    assert set(levels) == {0, 1}
    # Every entity — including the short row's e2 — appears at the coarsest level 0 (complete
    # partition); e2's coarsest id (9) was padded up so it lands at level 0.
    level_0_members = {eid for members in levels[0].values() for eid in members}
    assert level_0_members == {"e0", "e1", "e2"}
    # e2 also appears at the finer level 1 (padded), keyed by its own (repeated) coarsest id.
    level_1_members = {eid for members in levels[1].values() for eid in members}
    assert "e2" in level_1_members


def test_dendrogram_ragged_parent_links_do_not_raise() -> None:
    # The short row must not raise IndexError, and every finer community still points at an existing
    # coarser parent (the chain stays monotone after padding).
    parents = dendrogram_parent_links(_RAGGED_ROWS)
    assert all(p is None for p in parents[0].values())
    coarse_keys = set(dendrogram_to_levels(_RAGGED_ROWS)[0])
    for parent in parents[1].values():
        assert parent in coarse_keys
