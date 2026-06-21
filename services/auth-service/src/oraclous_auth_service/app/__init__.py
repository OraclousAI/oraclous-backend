"""FastAPI app factory for the auth-service (R1-A2).

The factory accepts the agent repository and the internal-service-key verifier
as dependencies so the endpoints are testable without a real database or Redis.
Production wiring (Postgres-backed credential store, Redis pipeline on
``app.state.redis``, real internal-key loader) lives in the service entrypoint
and is exercised at integration time, not here.
"""

from __future__ import annotations

from oraclous_auth_service.app.factory import create_app

__all__ = ["create_app"]
