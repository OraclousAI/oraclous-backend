"""OHM-v1 manifest validation (ORAA-4 §21 domain layer) — pure, no I/O.

Validation happens at PERSIST time (registration), not at allocation: a malformed descriptor never
reaches the table. The decisive rule (recon T2/T8): a ``oauth_token`` credential requirement must
declare non-empty ``scopes`` — a tool that asks for an OAuth token without naming its scopes would
later force the executor to request broad/unknown scope, so it is rejected fail-closed here.
"""

from __future__ import annotations

from typing import Any

from oraclous_capability_registry_service.domain.errors import InvalidDescriptorError
from oraclous_capability_registry_service.models.enums import DescriptorKind

_CREDENTIAL_TYPES = frozenset({"oauth_token", "api_key", "connection_string", "username_password"})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise InvalidDescriptorError(message)


def _validate_credential_requirements(requirements: Any) -> None:
    _require(isinstance(requirements, list), "spec.credential_requirements must be a list")
    for i, req in enumerate(requirements):
        _require(isinstance(req, dict), f"credential_requirements[{i}] must be an object")
        ctype = req.get("type")
        _require(
            ctype in _CREDENTIAL_TYPES,
            f"credential_requirements[{i}].type must be one of {sorted(_CREDENTIAL_TYPES)}",
        )
        if ctype == "oauth_token":
            _require(
                bool(req.get("provider")),
                f"credential_requirements[{i}] (oauth_token) must name a provider",
            )
            scopes = req.get("scopes")
            _require(
                isinstance(scopes, list) and len(scopes) > 0,
                f"credential_requirements[{i}] (oauth_token) must declare non-empty scopes",
            )


def validate_descriptor(kind: DescriptorKind, descriptor: Any) -> None:
    """Validate an OHM descriptor for the given kind; raise ``InvalidDescriptorError`` if malformed.

    Tool descriptors must carry a ``spec`` object; if it declares ``credential_requirements`` each
    one is shape- and scope-checked. Non-tool kinds (skill/agent/harness/human_role) only require a
    well-formed object — their richer validation belongs to the harness-runtime (R4).
    """
    _require(isinstance(descriptor, dict), "descriptor must be a JSON object")
    if kind != DescriptorKind.TOOL:
        return
    spec = descriptor.get("spec")
    _require(isinstance(spec, dict), "a tool descriptor must carry a 'spec' object")
    if "credential_requirements" in spec:
        _validate_credential_requirements(spec["credential_requirements"])


def descriptor_name(descriptor: dict[str, Any]) -> str | None:
    """Extract ``metadata.name`` for the denormalised search column (best-effort)."""
    metadata = descriptor.get("metadata")
    if isinstance(metadata, dict):
        name = metadata.get("name")
        if isinstance(name, str) and name:
            return name[:255]
    return None


def required_credential_types(descriptor: dict[str, Any]) -> list[str]:
    """The distinct *required* credential types a tool descriptor declares (order-stable)."""
    spec = descriptor.get("spec")
    if not isinstance(spec, dict):
        return []
    out: list[str] = []
    for req in spec.get("credential_requirements", []) or []:
        if not isinstance(req, dict):
            continue
        if req.get("required", True) and isinstance(req.get("type"), str):
            ctype = req["type"]
            if ctype not in out:
                out.append(ctype)
    return out
