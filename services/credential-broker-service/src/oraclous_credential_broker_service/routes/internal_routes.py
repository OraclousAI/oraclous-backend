"""Internal (service-to-service) routes (ORAA-4 §21 routes layer).

X-Internal-Key-gated endpoints for trusted callers (capability-registry, harness-runtime). S2 adds
the provider catalogue; S3/S5b add runtime-token + delegation endpoints under the same gate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from oraclous_credential_broker_service.core.dependencies import verify_internal_key
from oraclous_credential_broker_service.domain.providers import DATA_SOURCE_CAPABILITIES

router = APIRouter(
    prefix="/internal", tags=["internal"], dependencies=[Depends(verify_internal_key)]
)


@router.get("/providers")
async def list_provider_catalogue() -> dict:
    """The static provider/data-source capability catalogue (internal-key gated)."""
    return {"providers": DATA_SOURCE_CAPABILITIES}
