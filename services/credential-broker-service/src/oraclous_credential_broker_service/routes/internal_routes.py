"""Internal (service-to-service) routes (ORAA-4 §21 routes layer).

X-Internal-Key-gated endpoints for trusted callers (capability-registry, harness-runtime). S2 adds
the provider catalogue; S3/S5b add runtime-token + delegation endpoints under the same gate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from oraclous_credential_broker_service.core.dependencies import (
    CredentialBrokerServiceDep,
    verify_internal_key,
)
from oraclous_credential_broker_service.domain.providers import (
    DATA_SOURCE_CAPABILITIES,
    required_scopes_for,
)
from oraclous_credential_broker_service.schema.credential_schema import (
    EnsureDataSourceInput,
    RuntimeTokenInput,
    RuntimeTokenResponse,
)
from oraclous_credential_broker_service.services.credential_broker_service import TokenResult

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
