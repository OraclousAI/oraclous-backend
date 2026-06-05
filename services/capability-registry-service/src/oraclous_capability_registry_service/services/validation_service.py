"""Execution-readiness validation (ORAA-4 §21 services layer; reshape of legacy
``oraclous-core-service/app/services/validation_service.py``).

Produces a structured readiness report for a tool instance: the tool descriptor exists, every
required credential type is mapped, and configuration is present when a schema demands it. In S3 the
credential check is *presence* (a mapping exists); live token resolution against the
credential-broker lands in S4. ``is_ready`` is true only when there are no blocking errors.
"""

from __future__ import annotations

import uuid

from oraclous_capability_registry_service.models.enums import InstanceStatus
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)
from oraclous_capability_registry_service.repositories.instance_repository import InstanceRepository
from oraclous_capability_registry_service.schema.instance_schema import ValidationReport
from oraclous_capability_registry_service.services.instance_manager import InstanceNotFoundError


class ValidationService:
    def __init__(
        self, *, instances: InstanceRepository, capabilities: CapabilityRepository
    ) -> None:
        self._instances = instances
        self._capabilities = capabilities

    async def validate_execution_readiness(
        self, *, instance_id: uuid.UUID, organisation_id: uuid.UUID
    ) -> ValidationReport:
        instance = await self._instances.get_by_id(instance_id, organisation_id)
        if instance is None:
            raise InstanceNotFoundError("instance not found")

        checks: dict[str, str] = {}
        errors: list[dict] = []
        action_items: list[dict] = []

        # 1. the tool descriptor still exists in the registry
        descriptor = await self._capabilities.get_by_id(instance.capability_id, organisation_id)
        if descriptor is None:
            checks["capability"] = "failed"
            errors.append(
                {
                    "type": "CAPABILITY_NOT_FOUND",
                    "message": f"capability {instance.capability_id} not found in the registry",
                    "severity": "critical",
                }
            )
        else:
            checks["capability"] = "passed"

        # 2. every required credential type is mapped (presence; live resolution is S4)
        required = list(instance.required_credentials or [])
        mappings = dict(instance.credential_mappings or {})
        missing = [c for c in required if c not in mappings]
        if missing:
            checks["credentials"] = "failed"
            for ctype in missing:
                errors.append(
                    {
                        "type": "CREDENTIAL_NOT_CONFIGURED",
                        "message": f"required credential '{ctype}' is not configured",
                        "severity": "critical",
                        "credential_type": ctype,
                    }
                )
                action_items.append(
                    {
                        "action": "configure_credential",
                        "credential_type": ctype,
                        "message": f"map a credential for '{ctype}' to make this instance ready",
                    }
                )
        else:
            checks["credentials"] = "passed"

        # 3. configuration present when the descriptor declares a config schema (warning-only)
        if descriptor is not None:
            spec = descriptor.descriptor.get("spec") or {}
            if spec.get("configuration_schema") and not instance.configuration:
                checks["configuration"] = "warning"
            else:
                checks["configuration"] = "passed"

        is_ready = len(errors) == 0
        status = InstanceStatus.READY if is_ready else InstanceStatus.CONFIGURATION_REQUIRED
        return ValidationReport(
            is_ready=is_ready,
            instance_id=instance_id,
            status=status,
            checks=checks,
            errors=errors,
            action_items=action_items,
        )
