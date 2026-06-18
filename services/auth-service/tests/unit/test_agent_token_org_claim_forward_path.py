"""Failing pin for the ORA-3 auth-side forward path (ORA-31 / R1-A2).

ORA-31's acceptance criterion: *"The agent JWT's ``organisation_id`` claim
satisfies the ORA-3 forward path (packages/governance prefers the claim when
present)."*

That contract has two ends:

* The substrate end (``packages/governance.resolve_organisation_context``)
  prefers an auth-issued ``organisation_id`` claim and skips membership lookup
  — already pinned by ``packages/governance/tests/unit/test_organisation_context.py``.
* The auth end (this test) — the JWT this story issues must produce a
  ``Principal`` whose ``organisation_id`` is exactly the claim. Without this
  pin the auth side could drift the claim name (``org_id`` vs ``organisation_id``,
  string vs UUID) and the substrate would silently fall back to membership.

This is a single end-to-end pin against both seams; the governance fail-closed
paths and the auth issuance shape are exercised in their own files.

Behavioural reference: Contract ORA-3 (Option B, ratified 28 May 2026).

RED until ``oraclous_auth_service.core.jwt_handler.create_agent_token`` exists.
"""

from __future__ import annotations

import os
import uuid

import pytest
from jose import jwt
from oraclous_governance import jwt_audience, jwt_issuer
from oraclous_governance.context import (
    OrganisationContext,
    Principal,
    PrincipalType,
    resolve_organisation_context,
)

pytestmark = pytest.mark.unit


class _UnreachableMembershipResolver:
    """A resolver that fails the test if it is ever consulted.

    The whole point of an auth-issued ``organisation_id`` claim is that the
    substrate does not need to look up membership. If governance touches this
    resolver, the auth side has drifted from the contract.
    """

    async def organisations_for(self, principal: Principal) -> list[uuid.UUID]:
        raise AssertionError(
            "membership resolver must not be consulted when the auth claim is present"
        )


def _read_token_settings() -> tuple[str, str]:
    return (
        os.environ.setdefault("JWT_SECRET", "test-secret-for-ora-31-not-production"),
        os.environ.setdefault("JWT_ALGORITHM", "HS256"),
    )


async def test_agent_token_org_claim_feeds_governance_resolver_without_membership() -> None:
    """End-to-end: token issuance → claim decode → governance resolve, no membership."""
    secret, algorithm = _read_token_settings()
    from oraclous_auth_service.core.jwt_handler import create_agent_token

    agent_uuid = uuid.uuid4()
    organisation_uuid = uuid.uuid4()

    token, _ = create_agent_token(agent_id=str(agent_uuid), organisation_id=str(organisation_uuid))
    # iss/aud are stamped on every token (#356); pass them so the decode succeeds.
    claims = jwt.decode(
        token, secret, algorithms=[algorithm], audience=jwt_audience(), issuer=jwt_issuer()
    )

    # Build a Principal exactly the way the gateway will: from the JWT claims.
    principal = Principal(
        principal_id=uuid.UUID(claims["sub"]),
        principal_type=PrincipalType.AGENT,
        organisation_id=uuid.UUID(claims["organisation_id"]),
    )

    ctx = await resolve_organisation_context(principal, resolver=_UnreachableMembershipResolver())

    assert isinstance(ctx, OrganisationContext)
    assert ctx.organisation_id == organisation_uuid
    assert ctx.principal_id == agent_uuid
    assert ctx.principal_type == PrincipalType.AGENT
