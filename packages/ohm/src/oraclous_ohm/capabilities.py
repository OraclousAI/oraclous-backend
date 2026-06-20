"""Capability-absence guard over OHM v1.1 members (issue #396, ADR-032).

A member's ``tools`` list is its authoritative capability ceiling — deny-by-default and never
widened. A member may use a capability only if it is in that ceiling. The runtime calls
``assert_capability_allowed`` at the single dispatch seam (no orchestrator / A2A / coordinator path
may grant a capability the member did not declare); these helpers are pure and I/O-free.
"""

from __future__ import annotations

from collections.abc import Iterable

from oraclous_ohm.errors import OHMCapabilityError
from oraclous_ohm.manifest import OHMManifest, OHMMember


def ceiling(member: OHMMember) -> frozenset[str]:
    """The member's declared capability ceiling."""
    return frozenset(member.tools)


def capability_allowed(member: OHMMember, requested: str) -> bool:
    """True iff ``requested`` is within the member's declared ceiling (deny-by-default)."""
    return requested in ceiling(member)


def assert_capability_allowed(member: OHMMember, requested: str) -> None:
    """Fail-closed dispatch guard: raise ``OHMCapabilityError`` if ``requested`` is outside the
    member's ceiling. This is the single check the runtime calls before dispatching a capability."""
    if not capability_allowed(member, requested):
        raise OHMCapabilityError(
            f"member '{member.role}' has no capability '{requested}' "
            f"(declared ceiling: {sorted(ceiling(member))})"
        )


def effective_capabilities(member: OHMMember, offered: Iterable[str]) -> set[str]:
    """The capabilities a member actually gets: ``offered`` capped by its ceiling. Bounds whatever a
    sub-harness / orchestrator offers — the ceiling can only narrow it, never widen."""
    return set(offered) & ceiling(member)


def assert_subharness_within_ceiling(member: OHMMember, sub: OHMManifest) -> None:
    """Fail-closed: every capability the member's sub-harness declares must be within the member's
    ``tools`` ceiling (the sub-harness can only narrow it, never widen). Raises
    ``OHMCapabilityError`` on the first binding outside the ceiling.

    This is the cross-member dispatch guard (ADR-032/035 §5): a team member's ``tools[]`` is the
    authoritative ceiling, and the per-member sub-harness the runtime actually executes builds its
    policy ceiling from *its own* ``capabilities[]`` — so without this check a coordinator or client
    could hand a member a capability it never declared simply by widening the sub-harness manifest.
    A tool-less member (empty ceiling) admits a tool-less sub-harness only."""
    for cap in sub.capabilities:
        assert_capability_allowed(member, cap.binding)
