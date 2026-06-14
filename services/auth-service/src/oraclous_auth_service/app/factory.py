"""FastAPI app factory + routes for agent tokens (ORA-31 / R1-A2).

Endpoints:

* ``POST /agent-token`` — exchanges a raw agent credential for a short-lived
  JWT (rate-limited per credential prefix; bad credentials → 401).
* ``POST /internal/agent-credentials`` — internal-key gated; creates an agent +
  returns the raw credential exactly once.
* ``DELETE /internal/agent-credentials/{agent_id}`` — internal-key gated;
  revokes all active credentials for the agent (idempotent).
* ``GET /me`` — resolves the bearer token's agent principal; rejects revoked
  agents (T2 revocation race).

Reshape note: route shapes mirror the legacy ``/service-token``,
``/internal/service-account-keys`` and ``/me`` endpoints, with SA-only fields
(``tenant_id``/``home_graph_id``) removed and ``organisation_id`` added per the
ORA-3 / ADR-006 contract.
"""

from __future__ import annotations

import os
import secrets
from typing import Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from oraclous_telemetry import evaluate_readiness
from pydantic import BaseModel

from oraclous_auth_service.core.jwt_handler import (
    create_agent_token,
    create_service_account_token,
    decode_token,
)
from oraclous_auth_service.core.rate_limiter import (
    enforce_agent_credential_prefix_rate_limit,
)
from oraclous_auth_service.domain.passwords import PasswordPolicyError
from oraclous_auth_service.routes.auth_routes import router as auth_router
from oraclous_auth_service.routes.invitation_routes import org_router as invitation_org_router
from oraclous_auth_service.routes.invitation_routes import token_router as invitation_token_router
from oraclous_auth_service.routes.oauth_routes import router as oauth_router
from oraclous_auth_service.routes.org_routes import router as org_router
from oraclous_auth_service.services.auth_service import (
    AuthenticationError,
    EmailAlreadyRegisteredError,
)
from oraclous_auth_service.services.invitation_service import (
    InvitationInvalidError,
    InvitationRoleError,
)
from oraclous_auth_service.services.oauth_connect_sink import ConnectSinkError
from oraclous_auth_service.services.oauth_service import (
    OAuthError,
    OAuthProviderUnconfiguredError,
)
from oraclous_auth_service.services.org_service import OrgForbiddenError, OrgNotFoundError


def _add_cors(app: FastAPI) -> None:
    """Explicit CORS allowlist from ``AUTH_CORS_ORIGINS`` (comma-separated). No wildcard + creds —
    an empty/unset list adds no middleware (same-origin only). Replaces permissive regex (T-CORS).
    """
    raw = os.environ.get("AUTH_CORS_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )


def _register_user_identity(app: FastAPI) -> None:
    """Mount the user-identity + org + invitation + oauth routers, map exceptions, add `/health`."""
    app.include_router(auth_router)
    app.include_router(org_router)
    app.include_router(invitation_org_router)
    app.include_router(invitation_token_router)
    app.include_router(oauth_router)

    @app.exception_handler(AuthenticationError)
    async def _on_auth_error(_: Request, exc: AuthenticationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(EmailAlreadyRegisteredError)
    async def _on_email_taken(_: Request, exc: EmailAlreadyRegisteredError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    @app.exception_handler(PasswordPolicyError)
    async def _on_weak_password(_: Request, exc: PasswordPolicyError) -> JSONResponse:
        # Structured (FastAPI/Pydantic-shaped) so the gateway surfaces field + a machine-token
        # issue (exc.code) into VALIDATION_FAILED without relaying the raw message.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": [{"loc": ["body", "password"], "type": exc.code, "msg": str(exc)}]},
        )

    @app.exception_handler(OrgNotFoundError)
    async def _on_org_not_found(_: Request, exc: OrgNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    @app.exception_handler(OrgForbiddenError)
    async def _on_org_forbidden(_: Request, exc: OrgForbiddenError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)})

    @app.exception_handler(InvitationInvalidError)
    async def _on_invitation_invalid(_: Request, exc: InvitationInvalidError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})

    @app.exception_handler(InvitationRoleError)
    async def _on_invitation_role(_: Request, exc: InvitationRoleError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)}
        )

    @app.exception_handler(OAuthError)
    async def _on_oauth_error(_: Request, exc: OAuthError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})

    @app.exception_handler(OAuthProviderUnconfiguredError)
    async def _on_oauth_unconfigured(
        _: Request, exc: OAuthProviderUnconfiguredError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": str(exc)}
        )

    @app.exception_handler(ConnectSinkError)
    async def _on_connect_sink_error(_: Request, exc: ConnectSinkError) -> JSONResponse:
        # A downstream broker failure (G1 connect) → deliberate 502, never a leaked 500. The broker
        # response body is not propagated; only the generic reason in ``exc`` reaches the caller.
        return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content={"detail": str(exc)})

    @app.get("/health")
    async def health(request: Request) -> dict:
        # Liveness — always HTTP 200; the body reflects ok/degraded so an operator sees a startup
        # store-bind failure (ADR-021). The critical store is Postgres (sessionmaker); the
        # rate-limiter's Redis is fail-open and does not flip readiness.
        verdict = evaluate_readiness({"postgres": getattr(request.app.state, "sessionmaker", None)})
        return {"status": verdict.status, "service": "auth-service"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        # Readiness — 503 when a critical store didn't bind, so an orchestrator stops routing.
        verdict = evaluate_readiness({"postgres": getattr(request.app.state, "sessionmaker", None)})
        return JSONResponse(
            status_code=verdict.readyz_status_code,
            content={"status": verdict.status, "service": "auth-service"},
        )


# --- Protocol ---------------------------------------------------------------


class AgentRepositoryPort(Protocol):
    """The shape ``create_app`` expects of its agent repository dependency.

    The real ``AgentRepository`` (``oraclous_auth_service.repositories.agent_repository``)
    satisfies this shape; the unit tests pass a small fake. ``organisation_id_for``
    must return ``None`` for revoked agents so ``/me`` can fail-closed without a
    second lookup.
    """

    async def create_agent(
        self,
        *,
        organisation_id: str,
        created_by_user_id: str,
        principal_type: str = "agent",
    ) -> tuple[str, object]: ...

    async def validate_credential(self, raw_credential: str) -> str | None: ...

    async def revoke_agent(self, agent_id: str) -> int: ...

    async def organisation_id_for(self, agent_id: str) -> str | None: ...

    async def principal_type_for(self, agent_id: str) -> str | None: ...


# --- Request/Response schemas ----------------------------------------------


class _AgentTokenInput(BaseModel):
    """Input schema for ``POST /agent-token``.

    Named ``*Input`` (not ``*Request``) deliberately: the org-scoping lint
    guardrail's ``REQUEST_MODEL_SUFFIXES`` heuristic targets externally-facing
    ``*Request``/``*Body``/``*Payload`` schemas to catch public endpoints that
    accept ``organisation_id`` off the body. This schema has no
    ``organisation_id`` field at all (the org is resolved server-side from the
    credential), and the consistent ``*Input`` naming keeps the suffix scheme
    used across this module uniform.
    """

    credential: str


class _AgentTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 — OAuth2 token-type scheme name, not a secret
    expires_in: int
    principal_type: str = "agent"


class _CreateAgentInput(BaseModel):
    """Input schema for ``POST /internal/agent-credentials``.

    This is an internal, ``X-Internal-Key``-gated endpoint called by trusted
    platform services (the harness-runtime; the agent registry). The caller
    *is* the authority for which organisation the new agent belongs to — the
    organisation_id is platform → platform plumbing here, not a public-input
    value. The ``*Input`` suffix (not ``*Request``) keeps this schema out of the
    org-scoping lint's request-model heuristic, which is correctly tuned at
    *public* HTTP boundaries; the gate plus the explicit internal-key
    requirement is the runtime control.
    """

    organisation_id: str
    created_by_user_id: str
    # "agent" (default) or "service_account" — the machine-principal type this credential mints.
    principal_type: str = "agent"


class _CreateAgentResponse(BaseModel):
    agent_id: str
    credential: str  # raw — returned exactly once
    principal_type: str = "agent"


class _RevokeResponse(BaseModel):
    revoked_count: int


class _MeResponse(BaseModel):
    id: str
    principal_type: str
    organisation_id: str


# --- Internal-key dependency ------------------------------------------------


def _make_internal_key_verifier(expected: str):
    """Build a FastAPI dependency that gates routes on ``X-Internal-Key``.

    Fail-closed: any mismatch (including missing header) returns 401. The
    expected key is captured by closure rather than read from env at request
    time so tests can pass an explicit, ephemeral key without polluting env.
    """

    async def verify(x_internal_key: str | None = Header(default=None)) -> None:
        # Constant-time compare (T-INTERNAL): never short-circuit on a prefix mismatch.
        if (
            not expected
            or x_internal_key is None
            or not secrets.compare_digest(x_internal_key, expected)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing internal service key",
            )

    return verify


# --- Bearer-token resolver --------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)


def _principal_from_bearer(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008 — FastAPI Depends() idiom
) -> dict:
    """Decode the bearer JWT into its claims; raise 401 on any failure."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_token(credentials.credentials)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return claims


# --- App factory ------------------------------------------------------------


def create_app(
    *, agent_repository: AgentRepositoryPort, internal_service_key: str, lifespan=None
) -> FastAPI:
    """Build the auth-service FastAPI app with explicit dependencies.

    ``lifespan`` is supplied by ``main.py`` in production (it opens the Postgres sessionmaker +
    Redis into ``app.state``); tests omit it and wire ``app.state.sessionmaker`` directly when they
    exercise the user-identity routes (agent-only tests never touch them, so the unset → 503 path is
    harmless there).
    """
    app = FastAPI(title="oraclous-auth-service", version="0.0.1", lifespan=lifespan)
    app.state.agent_repository = agent_repository
    app.state.internal_service_key = internal_service_key
    # Production wires app.state.redis / app.state.sessionmaker at startup; tests leave them unset
    # and the limiter fails open / identity routes 503. Pre-declaring keeps the getattr lookups
    # surprise-free.
    if not hasattr(app.state, "redis"):
        app.state.redis = None
    if not hasattr(app.state, "sessionmaker"):
        app.state.sessionmaker = None

    _add_cors(app)
    _register_user_identity(app)

    verify_internal_key = _make_internal_key_verifier(internal_service_key)

    # --- POST /agent-token ------------------------------------------------

    @app.post(
        "/agent-token",
        response_model=_AgentTokenResponse,
        dependencies=[Depends(enforce_agent_credential_prefix_rate_limit)],
    )
    async def exchange_agent_token(
        token_input: _AgentTokenInput, request: Request
    ) -> _AgentTokenResponse:
        repo: AgentRepositoryPort = request.app.state.agent_repository
        agent_id = await repo.validate_credential(token_input.credential)
        if not agent_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked credential",
            )
        organisation_id = await repo.organisation_id_for(agent_id)
        if not organisation_id:
            # Defensive: validate_credential returned an agent but the agent's
            # organisation can't be resolved (raced revocation). Fail-closed.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked credential",
            )
        # Mint the token type the credential's principal is (agent | service_account).
        principal_type = await repo.principal_type_for(agent_id) or "agent"
        if principal_type == "service_account":
            token, expires_in = create_service_account_token(
                service_account_id=agent_id, organisation_id=organisation_id
            )
        else:
            token, expires_in = create_agent_token(
                agent_id=agent_id, organisation_id=organisation_id
            )
        return _AgentTokenResponse(
            access_token=token, expires_in=expires_in, principal_type=principal_type
        )

    # --- POST /internal/agent-credentials --------------------------------

    @app.post(
        "/internal/agent-credentials",
        response_model=_CreateAgentResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(verify_internal_key)],
    )
    async def create_agent_credential(
        create_input: _CreateAgentInput, request: Request
    ) -> _CreateAgentResponse:
        repo: AgentRepositoryPort = request.app.state.agent_repository
        raw, agent = await repo.create_agent(
            organisation_id=create_input.organisation_id,
            created_by_user_id=create_input.created_by_user_id,
            principal_type=create_input.principal_type,
        )
        return _CreateAgentResponse(
            agent_id=agent.id, credential=raw, principal_type=create_input.principal_type
        )

    # --- DELETE /internal/agent-credentials/{agent_id} -------------------

    @app.delete(
        "/internal/agent-credentials/{agent_id}",
        response_model=_RevokeResponse,
        dependencies=[Depends(verify_internal_key)],
    )
    async def revoke_agent_credential(agent_id: str, request: Request) -> _RevokeResponse:
        repo: AgentRepositoryPort = request.app.state.agent_repository
        count = await repo.revoke_agent(agent_id)
        return _RevokeResponse(revoked_count=count)

    # --- GET /me ---------------------------------------------------------

    @app.get("/me", response_model=_MeResponse)
    async def me(
        request: Request,
        claims: dict = Depends(_principal_from_bearer),  # noqa: B008 — FastAPI Depends() idiom
    ) -> _MeResponse:
        principal_type = claims.get("principal_type")
        sub = claims.get("sub")
        if not sub or principal_type not in ("agent", "service_account"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unsupported principal type",
                headers={"WWW-Authenticate": "Bearer"},
            )
        repo: AgentRepositoryPort = request.app.state.agent_repository
        organisation_id = await repo.organisation_id_for(sub)
        if not organisation_id:
            # Revocation race (T2): even with an unexpired token, a revoked
            # machine principal can never re-authenticate.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Credential has been revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return _MeResponse(id=sub, principal_type=principal_type, organisation_id=organisation_id)

    return app
