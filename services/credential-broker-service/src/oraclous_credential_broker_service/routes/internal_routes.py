"""Internal (service-to-service) routes (ORAA-4 §21 routes layer).

X-Internal-Key-gated endpoints for trusted callers (capability-registry, harness-runtime). S2 adds
the provider catalogue; S3/S5b add runtime-token + delegation endpoints under the same gate.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from oraclous_credential_broker_service.core.dependencies import (
    CredentialBrokerServiceDep,
    CredentialServiceDep,
    DelegationServiceDep,
    WebhookSecretServiceDep,
    verify_internal_key,
)
from oraclous_credential_broker_service.domain.providers import (
    DATA_SOURCE_CAPABILITIES,
    required_scopes_for,
)
from oraclous_credential_broker_service.schema.credential_schema import (
    DelegatedTokenMintResponse,
    DelegationValidationResponse,
    EnsureDataSourceInput,
    MintDelegatedTokenInput,
    ResolveCredentialInput,
    ResolveCredentialResponse,
    RevokeDelegatedTokenInput,
    RuntimeTokenInput,
    RuntimeTokenResponse,
    ValidateDelegatedTokenInput,
)
from oraclous_credential_broker_service.schema.webhook_secret_schema import (
    WebhookSecretMintInput,
    WebhookSecretMintResponse,
    WebhookSecretResolveInput,
    WebhookSecretResolveResponse,
)
from oraclous_credential_broker_service.services.credential_broker_service import TokenResult
from oraclous_credential_broker_service.services.webhook_secret_service import WebhookSecretNotFound

router = APIRouter(
    prefix="/internal", tags=["internal"], dependencies=[Depends(verify_internal_key)]
)


def _to_response(result: TokenResult) -> RuntimeTokenResponse:
    return RuntimeTokenResponse(
        success=result.success,
        access_token=result.access_token,
        expires_at=result.expires_at,
        scopes=result.scopes,
        provider=result.provider,
        error_code=result.error_code,
        missing_scopes=result.missing_scopes,
        login_url=result.login_url,
    )


@router.get("/providers")
async def list_provider_catalogue() -> dict:
    """The static provider/data-source capability catalogue (internal-key gated)."""
    return {"providers": DATA_SOURCE_CAPABILITIES}


# NOTE: these are X-Internal-Key-gated service-to-service endpoints — the trusted caller
# (capability-registry / harness-runtime) supplies organisation_id. Handler params are named
# ``*_input`` (not ``body``/``payload``) so the ORG001 public-body heuristic, correctly tuned for
# public endpoints, doesn't fire here (the internal-key gate is the control). Same idiom as the
# auth-service ``_CreateAgentInput`` internal endpoint.
@router.post("/runtime-token", response_model=RuntimeTokenResponse)
async def runtime_token(
    runtime_input: RuntimeTokenInput, broker: CredentialBrokerServiceDep
) -> RuntimeTokenResponse:
    result = await broker.get_provider_token(
        organisation_id=runtime_input.organisation_id,
        user_id=runtime_input.user_id,
        provider=runtime_input.provider,
        required_scopes=runtime_input.required_scopes,
    )
    return _to_response(result)


@router.post("/ensure-data-source-access", response_model=RuntimeTokenResponse)
async def ensure_data_source_access(
    access_input: EnsureDataSourceInput, broker: CredentialBrokerServiceDep
) -> RuntimeTokenResponse:
    # Required scopes come from the catalogue for the named data source (never trusted input).
    required = required_scopes_for(access_input.provider, access_input.data_source)
    result = await broker.get_provider_token(
        organisation_id=access_input.organisation_id,
        user_id=access_input.user_id,
        provider=access_input.provider,
        required_scopes=required,
    )
    return _to_response(result)


# --- delegated-token API (S5b): wires the shipped delegation service + store to HTTP ---
@router.post("/delegated-tokens", response_model=DelegatedTokenMintResponse)
async def mint_delegated_token(
    mint_input: MintDelegatedTokenInput, delegation: DelegationServiceDep
) -> DelegatedTokenMintResponse:
    raw, record = await delegation.mint(
        organisation_id=mint_input.organisation_id,
        member_id=mint_input.member_id,
        agent_id=mint_input.agent_id,
        scopes=mint_input.scopes,
        expires_at=mint_input.expires_at,
    )
    return DelegatedTokenMintResponse(
        token=raw,
        token_id=record.id,
        member_id=record.member_id,
        agent_id=record.agent_id,
        scopes=sorted(record.scopes),
        expires_at=record.expires_at,
    )


@router.post("/delegated-tokens/validate", response_model=DelegationValidationResponse)
async def validate_delegated_token(
    validate_input: ValidateDelegatedTokenInput, delegation: DelegationServiceDep
) -> DelegationValidationResponse:
    v = await delegation.validate(
        raw_token=validate_input.raw_token,
        organisation_id=validate_input.organisation_id,
        requesting_agent_id=validate_input.requesting_agent_id,
        requested_scopes=validate_input.requested_scopes,
    )
    return DelegationValidationResponse(
        success=v.success,
        reason=v.reason,
        token_id=v.token_id,
        member_id=v.member_id,
        agent_id=v.agent_id,
        granted_scopes=sorted(v.granted_scopes) if v.granted_scopes is not None else None,
    )


@router.post("/delegated-tokens/{token_id}/revoke")
async def revoke_delegated_token(
    token_id: UUID, revoke_input: RevokeDelegatedTokenInput, delegation: DelegationServiceDep
) -> dict:
    count = await delegation.revoke(token_id=token_id, organisation_id=revoke_input.organisation_id)
    return {"revoked_count": count}


# --- non-OAuth credential resolution for trusted services (capability-registry tool execution) ---
@router.post("/resolve-credential", response_model=ResolveCredentialResponse)
async def resolve_credential(
    resolve_input: ResolveCredentialInput, svc: CredentialServiceDep
) -> ResolveCredentialResponse:
    """Return a stored credential's DECRYPTED payload by id (org-scoped). For service→service
    resolution of non-OAuth secrets (connection_string / api_key) used by tool execution; the
    OAuth flow stays on ``/runtime-token``. CredentialNotFoundError → 404 (cross-org mask)."""
    out = await svc.resolve_decrypted(
        credential_id=resolve_input.credential_id, organisation_id=resolve_input.organisation_id
    )
    return ResolveCredentialResponse(
        credential_id=out.id,
        provider=out.provider,
        cred_type=out.cred_type,
        credential=out.credential,
    )


@router.post("/webhook-secrets", response_model=WebhookSecretMintResponse)
async def mint_webhook_secret(
    mint_input: WebhookSecretMintInput, svc: WebhookSecretServiceDep
) -> WebhookSecretMintResponse:
    """Mint a per-webhook HMAC signing secret for an org (encrypted at rest). The gateway keeps only
    the returned id (the reference); the plaintext never leaves the broker (R6 Slice 7, ADR-008)."""
    secret_id = await svc.mint(organisation_id=mint_input.organisation_id, secret=mint_input.secret)
    return WebhookSecretMintResponse(secret_id=secret_id)


@router.post("/webhook-secrets/resolve", response_model=WebhookSecretResolveResponse)
async def resolve_webhook_secret(
    resolve_input: WebhookSecretResolveInput, svc: WebhookSecretServiceDep
) -> WebhookSecretResolveResponse:
    """Return a webhook secret's DECRYPTED value by id (org-scoped) for the gateway to
    recompute the inbound HMAC. WebhookSecretNotFound → 404 (cross-org mask)."""
    try:
        secret = await svc.resolve(
            secret_id=resolve_input.secret_id, organisation_id=resolve_input.organisation_id
        )
    except WebhookSecretNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no such webhook secret"
        ) from exc
    return WebhookSecretResolveResponse(secret=secret)
