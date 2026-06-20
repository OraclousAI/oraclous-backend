"""Capability-absence: a team member's tools[] is a hard ceiling (issue #396, ADR-032).

A member may use a capability only if it is in the member's declared ``tools`` ceiling. The ceiling
is deny-by-default (an empty ``tools`` permits nothing) and is never widened — the structural
basis of the book studio's author gates: an agent imported with ``tools: Read,Grep,Glob,Write``
literally cannot publish. The runtime wires ``assert_capability_allowed`` into the single dispatch
seam (E3); these tests pin the pure guard.
"""

from __future__ import annotations

import pytest
from oraclous_ohm.capabilities import (
    assert_capability_allowed,
    assert_subharness_within_ceiling,
    capability_allowed,
    ceiling,
    effective_capabilities,
)
from oraclous_ohm.errors import OHMCapabilityError
from oraclous_ohm.manifest import (
    OHMActor,
    OHMCapability,
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMRuntime,
)


def _m(tools: list[str]) -> OHMMember:
    return OHMMember(role="r", kind="agent", manifest_ref="org:x/a@1", tools=tools)


def _sub(bindings: list[str]) -> OHMManifest:
    import uuid

    return OHMManifest(
        ohm_version="1.0",
        metadata=OHMMetadata(id=uuid.uuid4(), name="s", owner_organization_id=uuid.uuid4()),
        capabilities=[OHMCapability(ref=f"core/{b}@1", binding=b) for b in bindings],
        actors=[OHMActor(role="primary", kind="agent")],
        runtime=OHMRuntime(entrypoint="primary"),
    )


def test_member_tools_ceiling_defaults_empty() -> None:
    m = OHMMember(role="r", kind="agent", manifest_ref="org:x/a@1")
    assert m.tools == []


def test_capability_allowed_only_when_in_ceiling() -> None:
    m = _m(["web.search", "web.fetch"])
    assert capability_allowed(m, "web.search") is True
    assert capability_allowed(m, "graph_ingest") is False


def test_empty_ceiling_denies_everything() -> None:
    m = _m([])
    assert capability_allowed(m, "anything") is False


def test_assert_is_fail_closed() -> None:
    m = _m(["read"])
    assert assert_capability_allowed(m, "read") is None  # allowed → no raise
    with pytest.raises(OHMCapabilityError):
        assert_capability_allowed(m, "publish")


def test_ceiling_is_a_frozenset() -> None:
    assert ceiling(_m(["a", "b", "a"])) == frozenset({"a", "b"})


def test_effective_capabilities_is_offered_capped_by_ceiling() -> None:
    m = _m(["read", "search"])
    assert effective_capabilities(m, {"read", "search", "publish"}) == {"read", "search"}
    assert effective_capabilities(_m([]), {"read"}) == set()


def test_book_drafting_agent_cannot_publish() -> None:
    drafter = _m(["Read", "Grep", "Glob", "Write"])
    assert capability_allowed(drafter, "Write") is True
    for forbidden in ("publish_to_kdp", "send_email", "spend"):
        assert capability_allowed(drafter, forbidden) is False


def test_subharness_within_ceiling_passes() -> None:
    # a sub-harness whose capabilities are a subset of the member's tools ceiling is allowed
    assert assert_subharness_within_ceiling(_m(["Read", "Write"]), _sub(["Read"])) is None


def test_subharness_exceeding_ceiling_is_fail_closed() -> None:
    # the cross-member guard (ADR-035 §5): a sub-harness cannot widen the member past its tools[]
    with pytest.raises(OHMCapabilityError):
        assert_subharness_within_ceiling(_m(["Read"]), _sub(["Read", "shell"]))


def test_tool_less_member_admits_only_a_tool_less_subharness() -> None:
    assert assert_subharness_within_ceiling(_m([]), _sub([])) is None  # empty ⊆ empty
    with pytest.raises(OHMCapabilityError):
        assert_subharness_within_ceiling(_m([]), _sub(["anything"]))  # empty ceiling denies all
