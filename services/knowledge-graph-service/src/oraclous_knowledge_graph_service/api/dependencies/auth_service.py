"""Auth-service dependency for knowledge-graph-service API (ORAA-55).

Verifies bearer tokens issued by the auth-service. Patched by the test suite
(ORA-48 / test_api_authz_isolation.py) at the module level:
  oraclous_knowledge_graph_service.api.dependencies.auth_service

Fail-closed: any missing or unverifiable token raises HTTP 401.
"""

from __future__ import annotations

from fastapi import HTTPException


async def verify_token(token: str) -> dict:
    """Verify a bearer token and return the user dict.

    Real implementation delegates to the auth-service JWT handler.
    Raises HTTPException(401) for any unverifiable token.
    """
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    raise HTTPException(status_code=401, detail="Token verification not implemented")
