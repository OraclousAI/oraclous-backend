"""Execution-readiness validation (services layer; reshape of legacy
``oraclous-core-service/app/services/validation_service.py``).

Produces a structured readiness report for a tool instance: the tool descriptor exists, every
required credential resolves through the broker, and configuration is present when a schema demands
it. ``is_ready`` is true only when there are no blocking errors.

The credential check used to be *presence* only — a mapping exists. #692 is what that costs: a
credential row was deleted, the mapping that pointed at it survived, and this report kept saying
READY while the console's "Test connection" said Healthy. The truth only appeared 50k tokens into a
run, as a broker 404 per call. A check that reports Healthy for an instance that cannot run is worse
than no check, so the mapped credential is now RESOLVED here, exactly as the execution spine
resolves it, and a failure both fails the report and moves the instance out of ``READY`` so the
console renders it as needing attention.

Why the status is refreshed here rather than cascaded from the credential-broker on delete: the
broker is Layer 1 and the instance table belongs to the capability registry, so clearing the
mappings on delete would be an upward cross-service write (ADR-001). Recomputing the status where
the instance lives needs no cascade and cannot go stale in the other direction either — a
reconnected credential restores ``READY`` on the next check.
"""

from __future__ import annotations

import logging
import uuid

from oraclous_capability_registry_service.domain.credentials import required_credentials
from oraclous_capability_registry_service.models.enums import InstanceStatus
from oraclous_capability_registry_service.repositories.capability_repository import (
    CapabilityRepository,
)
from oraclous_capability_registry_service.repositories.instance_repository import InstanceRepository
from oraclous_capability_registry_service.schema.instance_schema import ValidationReport
from oraclous_capability_registry_service.services.credential_client import (
    CredentialBrokerPort,
    CredentialResolutionError,
)
from oraclous_capability_registry_service.services.instance_manager import InstanceNotFoundError

#: The statuses this check owns. A run leaves RUNNING/SUCCESS/FAILED behind, and a readiness poll
#: must not erase that record — an instance in one of those states is already not READY, which is
#: all #692 AC1 asks for.
_REFRESHABLE = (InstanceStatus.READY, InstanceStatus.CONFIGURATION_REQUIRED)

logger = logging.getLogger(__name__)


class ValidationService:
    def __init__(
        self,
        *,
        instances: InstanceRepository,
        capabilities: CapabilityRepository,
        broker: CredentialBrokerPort | None,
    ) -> None:
        self._instances = instances
        self._capabilities = capabilities
        self._broker = broker

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

        # 2. every required credential type is mapped AND resolves through the broker
        required = list(instance.required_credentials or [])
        mappings = dict(instance.credential_mappings or {})
        missing = [c for c in required if c not in mappings]
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
        unverified = False
        if descriptor is not None:
            unresolvable, unverified = await self._unresolvable(
                descriptor.descriptor,
                mappings=mappings,
                skip=set(missing),
                organisation_id=organisation_id,
                user_id=instance.user_id,
            )
            for ctype, reason in unresolvable:
                errors.append(
                    {
                        "type": "CREDENTIAL_UNRESOLVABLE",
                        # The reason is the broker's TYPED code, never its message and never the
                        # credential id (#483 envelope discipline) — it says what to do, not what
                        # was stored.
                        "message": (
                            f"the credential connected for '{ctype}' could not be resolved "
                            f"({reason}) — reconnect it"
                        ),
                        "severity": "critical",
                        "credential_type": ctype,
                    }
                )
                action_items.append(
                    {
                        "action": "configure_credential",
                        "credential_type": ctype,
                        "message": f"reconnect a working credential for '{ctype}'",
                    }
                )
        if any(e["type"].startswith("CREDENTIAL_") for e in errors):
            checks["credentials"] = "failed"
        elif unverified:
            # The broker itself could not be reached, so there is no answer either way. Saying
            # "passed" would be the #692 lie again; raising a blocking error would flip every
            # instance in the org to CONFIGURATION_REQUIRED over one upstream blip. A warning is
            # the honest third answer, and it writes no status.
            checks["credentials"] = "warning"
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
        await self._refresh_status(
            instance, instance_id=instance_id, organisation_id=organisation_id, status=status
        )
        return ValidationReport(
            is_ready=is_ready,
            instance_id=instance_id,
            status=status,
            checks=checks,
            errors=errors,
            action_items=action_items,
        )

    async def _unresolvable(
        self,
        descriptor: dict,
        *,
        mappings: dict[str, str],
        skip: set[str],
        organisation_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> tuple[list[tuple[str, str]], bool]:
        """``([(credential_type, broker error code)], the broker was unreachable)``.

        This is the same resolve the execution spine performs, so "Healthy" and "runs" can no longer
        disagree. Three cases are kept apart:

        * A requirement with no mapping at all is skipped — already reported as
          CREDENTIAL_NOT_CONFIGURED, and resolving it would add a second error for one problem.
        * An ``oauth_token`` requirement is skipped. The broker mints it at execute time from
          (org, user, provider, scopes) and ignores ``credential_mappings``, so there is no stored
          row to dangle — and minting a token as the side effect of a readiness GET would be a
          side effect this endpoint has no business having.
        * The broker being unreachable is NOT the same as the broker saying no. It returns the
          second element instead of a failure, so a transient upstream cannot mass-downgrade the
          org's instances.
        """
        checkable = [
            r
            for r in required_credentials(descriptor)
            if isinstance(r.get("type"), str)
            and r["type"] not in skip
            and r["type"] != "oauth_token"
        ]
        if self._broker is None:  # degraded startup: no broker bound → nothing can be verified
            return [], bool(checkable)
        failures: list[tuple[str, str]] = []
        unreachable = False
        for requirement in checkable:
            ctype = str(requirement["type"])
            try:
                await self._broker.resolve(
                    organisation_id=organisation_id,
                    user_id=user_id,
                    requirement=requirement,
                    credential_id=mappings.get(ctype),
                )
            except CredentialResolutionError as exc:
                failures.append((ctype, exc.error_code))
            except Exception:  # noqa: BLE001 — transport/timeout: no answer, never a verdict
                logger.warning(
                    "credential-broker unreachable during readiness; credential check unverified"
                )
                unreachable = True
        return failures, unreachable

    async def _refresh_status(
        self,
        instance,  # noqa: ANN001 — the ToolInstance row, kept untyped to avoid a models import here
        *,
        instance_id: uuid.UUID,
        organisation_id: uuid.UUID,
        status: InstanceStatus,
    ) -> None:
        """Persist the recomputed status, so the console's instance list agrees with this report.

        Written only when it actually changes: readiness is polled, and a write per poll is churn.
        The credential mappings are passed through untouched — clearing them is the cross-service
        cascade this deliberately does not build.
        """
        if instance.status == status or instance.status not in _REFRESHABLE:
            return
        await self._instances.set_credentials_and_status(
            instance_id, organisation_id, dict(instance.credential_mappings or {}), status
        )
