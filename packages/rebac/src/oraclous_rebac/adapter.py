"""Adapter wiring ``ReBACEngine`` into the substrate ``AccessDecisionClient``
seam as a production resolver.

Satisfies the substrate ``RelationResolver`` protocol — ``async def resolve(
AccessRequest) -> bool | None`` — by dispatching to
``ReBACEngine.check_graph_permission``. The seam remains the single fail-closed
chokepoint; this adapter:

* maps the seam vocabulary onto the engine's: ``organisation_id`` →
  ``organisation_id``, ``subject`` → ``subject`` (as ``{"type": "user",
  "id": <value>}``), ``resource`` → ``graph_id``,
  ``relation`` → ``required_level`` via a defined ``{read, write, admin}``
  lookup;
* returns ``None`` for unknown relations and for out-of-domain subjects /
  resources, so the seam fails closed without a best-effort engine call;
* does not pre-collapse engine exceptions to ``False`` — they propagate to the
  seam, which converts them into ``AccessDecision.deny`` with a *resolver
  error* reason, distinct from a definitive *absent* deny.

Production *injection* (wiring this adapter into a live request path) is out
of scope here — it lands with the first real consumer at R3/R6 — so the
adapter is engine-agnostic in its callable: tests inject a stub
``permission_check``, production injects ``engine.check_graph_permission``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from oraclous_substrate.rebac import AccessRequest

# The defined relation→required_level lookup. Not identity, deliberately —
# adding a relation requires editing this table, which is the brief's
# "defined lookup, not identity" emphasis. The three values match the engine's
# ``_ACCEPTABLE_LEVELS`` keys.
_RELATION_TO_LEVEL: dict[str, str] = {
    "read": "read",
    "write": "write",
    "admin": "admin",
}

# Subject and resource type discrimination. The seam's vocabulary uses
# ``user-<id>`` and ``graph-<id>`` (case-sensitive, lowercased) — see
# ``packages/substrate/tests/unit/test_rebac_client.py``. Agent subjects
# and non-graph resource types get their own resolvers; this
# adapter fails closed on anything else.
_USER_SUBJECT_PREFIX = "user-"
_GRAPH_RESOURCE_PREFIX = "graph-"


class _PermissionCheck(Protocol):
    """The callable shape this adapter dispatches into.

    Matches ``ReBACEngine.check_graph_permission`` — a positional driver, then
    keyword-only org / user / graph / level — so a wiring mistake (positional
    misalignment, wrong kwarg names) surfaces at TypeError instead of as a
    silent mis-authorisation.
    """

    async def __call__(
        self,
        driver: object,
        *,
        organisation_id: str,
        subject: dict[str, str],
        graph_id: str,
        required_level: str,
    ) -> bool: ...


class _OwnerOrgCheck(Protocol):
    """Matches ``ReBACEngine.grant_owner_org`` — returns the owner org recorded on the caller's
    grant for a graph (ADR-036), or None. Positional driver, keyword-only org / graph / subject."""

    async def __call__(
        self,
        driver: object,
        *,
        organisation_id: str,
        graph_id: str,
        subject: dict[str, str],
    ) -> str | None: ...


class ReBACEngineResolver:
    """Substrate-seam-compatible resolver backed by ``ReBACEngine``."""

    def __init__(
        self,
        *,
        permission_check: _PermissionCheck,
        driver: object | None = None,
        owner_org_check: _OwnerOrgCheck | None = None,
    ) -> None:
        self._permission_check = permission_check
        self._driver = driver
        # Optional (ADR-036): when wired, the seam reads back the owner org of a granted cross-org
        # graph so federation can bind it. Absent → no owner org surfaces (consumer fail-closes).
        self._owner_org_check = owner_org_check

    async def resolve(self, request: AccessRequest) -> bool | None:
        if not request.subject.startswith(_USER_SUBJECT_PREFIX):
            return None
        if not request.resource.startswith(_GRAPH_RESOURCE_PREFIX):
            return None
        required_level = _RELATION_TO_LEVEL.get(request.relation)
        if required_level is None:
            return None

        return await self._permission_check(
            self._driver,
            organisation_id=request.organisation_id,
            subject={"type": "user", "id": request.subject},
            graph_id=request.resource,
            required_level=required_level,
        )

    async def resolve_owner_org(self, request: AccessRequest) -> str | None:
        """The owner org of the caller's grant for the requested graph (ADR-036), or None. Only ever
        called by the seam AFTER ``resolve`` allows; same fail-closed subject/resource guards."""
        if self._owner_org_check is None:
            return None
        if not request.subject.startswith(_USER_SUBJECT_PREFIX):
            return None
        if not request.resource.startswith(_GRAPH_RESOURCE_PREFIX):
            return None
        return await self._owner_org_check(
            self._driver,
            organisation_id=request.organisation_id,
            graph_id=request.resource,
            subject={"type": "user", "id": request.subject},
        )
