"""Substrate ReBAC access-decision seam (Layer 1).

The thin client higher layers call to ask "may this subject perform this
relation on this resource?". Fail-closed: any outcome that is not an explicit
grant is a denial (ADR-004 federation via ReBAC; Threat Catalogue T1-M2).
Tenancy is mandatory on every decision (ADR-006).

The concrete relation store is injected as a ``RelationResolver``. R0.5 carries
only the seam contract and its fail-closed mapping; the full policy model
(roles, permissions, subgraphs) is a later release.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AccessRequest:
    """A request to decide whether ``subject`` may ``relation`` ``resource``."""

    organisation_id: str
    subject: str
    resource: str
    relation: str


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """An explicit, typed allow/deny decision with a rationale."""

    allowed: bool
    reason: str = ""

    @classmethod
    def allow(cls, reason: str = "") -> AccessDecision:
        return cls(allowed=True, reason=reason)

    @classmethod
    def deny(cls, reason: str) -> AccessDecision:
        return cls(allowed=False, reason=reason)


class RelationResolver(Protocol):
    """Resolves whether a relation holds in the underlying ReBAC store.

    Returns ``True`` if the relation is present, ``False`` if it is definitively
    absent, and ``None`` if resolution is indeterminate.
    """

    async def resolve(self, request: AccessRequest) -> bool | None: ...


class AccessDecisionClient:
    """Fail-closed access-decision seam over a ``RelationResolver``."""

    def __init__(self, resolver: RelationResolver) -> None:
        self._resolver = resolver

    async def check(self, request: AccessRequest) -> AccessDecision:
        # ADR-006: every decision is parameterised by organisation_id; a missing
        # tenancy scope is a programming error, never an implicit allow.
        if not request.organisation_id or not request.organisation_id.strip():
            raise ValueError("organisation_id is required for an access decision")

        try:
            resolved = await self._resolver.resolve(request)
        except Exception:
            # A store error denies rather than propagating or allowing (T1-M2).
            return AccessDecision.deny("resolver error; failing closed")

        if resolved is True:
            return AccessDecision.allow("relation present")
        # Absent (False) or ambiguous (None) both deny — fail-closed default.
        return AccessDecision.deny("relation absent or ambiguous; failing closed")
