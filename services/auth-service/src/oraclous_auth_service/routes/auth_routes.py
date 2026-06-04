"""User-identity routes (ORAA-4 §21 routes layer).

Each handler parses the request, makes ONE `AuthService` call, and maps the result to a DTO.
Domain failures (`AuthenticationError`, `EmailAlreadyRegisteredError`, `PasswordPolicyError`) are
translated to HTTP by the exception handlers registered in `app/factory.py` — keeping handlers thin.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, status

from oraclous_auth_service.core.dependencies import AuthServiceDep, UserClaimsDep
from oraclous_auth_service.schema.auth_schemas import (
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
)
from oraclous_auth_service.services.auth_service import TokenBundle

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _token_response(bundle: TokenBundle) -> TokenResponse:
    return TokenResponse(
        access_token=bundle.access_token,
        refresh_token=bundle.refresh_token,
        expires_in=bundle.expires_in,
        email=bundle.email,
        is_superuser=bundle.is_superuser,
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, auth: AuthServiceDep) -> TokenResponse:
    return _token_response(await auth.register(email=body.email, password=body.password))


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    auth: AuthServiceDep,
    x_organisation_id: Annotated[str | None, Header(alias="X-Organisation-Id")] = None,
) -> TokenResponse:
    # X-Organisation-Id selects the active org for this session (validated against membership).
    bundle = await auth.login(
        email=body.email, password=body.password, requested_org_id=x_organisation_id
    )
    return _token_response(bundle)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, auth: AuthServiceDep) -> TokenResponse:
    return _token_response(await auth.refresh(refresh_token=body.refresh_token))


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest, claims: UserClaimsDep, auth: AuthServiceDep
) -> None:
    await auth.change_password(user_id=claims["sub"], new_password=body.new_password)


@router.get("/me", response_model=MeResponse)
async def me(claims: UserClaimsDep, auth: AuthServiceDep) -> MeResponse:
    user = await auth.get_user(user_id=claims["sub"])
    return MeResponse(
        id=user.id,
        principal_type="user",
        organisation_id=user.default_organisation_id,
        email=user.email,
    )


@router.get("/validate")
async def validate(claims: UserClaimsDep) -> dict:
    return {
        "valid": True,
        "sub": claims["sub"],
        "principal_type": claims["principal_type"],
        "organisation_id": claims["organisation_id"],
    }
