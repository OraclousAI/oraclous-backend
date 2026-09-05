"""The credential-preflight 409, in one place (routes layer).

Lifted out of ``team_run_routes`` when apps gained a second way to start a run (#932). Two routes
emitting the same refusal is fine; two routes emitting it in two SHAPES is not — the gateway relays
this by reading ``needs_credential`` at the TOP level of the body, so a copy that nested it, or
renamed it, would silently stop reaching the console's connect prompt.

A pure response builder: no business logic, no store access — the routes layer is allowed to shape
a response, and shaping it identically in both places is the whole point.
"""

from __future__ import annotations

from fastapi import status
from fastapi.responses import JSONResponse

from oraclous_execution_engine_service.services.team_run_service import TeamRunPreflightError


def preflight_409(exc: TeamRunPreflightError) -> JSONResponse:
    """#664: a credential miss at GO.

    The gateway relays a 409 as ``CREDENTIALS_REQUIRED`` only when ``needs_credential`` sits at the
    TOP level (``extract_needs_credential`` reads nothing nested), so this cannot ride an
    ``HTTPException`` — its payload would land under ``detail``. The body is the leak-safe pair,
    ``error_code`` for a DIRECT engine caller (the gateway's own allow-list does not carry
    ``CREDENTIALS_REQUIRED``, so the relay to the client rides ``needs_credential`` alone), and the
    human sentence for logs.
    """
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": str(exc),
            "error_code": "CREDENTIALS_REQUIRED",
            "needs_credential": exc.needs_credential,
        },
    )
