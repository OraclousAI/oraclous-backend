"""Adapter wiring ``ReBACEngine`` into the substrate ``AccessDecisionClient``
seam as a production resolver (ORA-46).

Satisfies the ORA-15 ``RelationResolver`` protocol — ``async def resolve(
AccessRequest) -> bool | None`` — by dispatching to
``ReBACEngine.check_graph_permission``. The seam remains the single fail-closed
chokepoint; this adapter:

* maps the seam vocabulary onto the engine's: ``organisation_id`` →
  ``organisation_id``, ``subject`` → ``user_id``, ``resource`` → ``graph_id``,
  ``relation`` → ``required_level`` via a defined ``{read, write, admin}``
  lookup;
* returns ``None`` for unknown relations and for out-of-domain subjects /
  resources, so the seam fails closed without a best-effort engine call;
* does not pre-collapse engine exceptions to ``False`` — they propagate to the
  seam, which converts them into ``AccessDecision.deny`` with a *resolver
  error* reason, distinct from a definitive *absent* deny.

Production *injection* (wiring this adapter into a live request path) is out
of scope for ORA-46 — it lands with the first real consumer at R3/R6 — so the
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
# (ORA-27 C2) and non-graph resource types get their own resolvers; this
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
        user_id: str,
        graph_id: str,
        required_level: str,
    ) -> bool: ...


class ReBACEngineResolver:
    """Substrate-seam-compatible resolver backed by ``ReBACEngine``."""

    def __init__(
        self,
        *,
        permission_check: _PermissionCheck,
        driver: object | None = None,
    ) -> None:
        self._permission_check = permission_check
        self._driver = driver

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
            user_id=request.subject,
            graph_id=request.resource,
            required_level=required_level,
        )
