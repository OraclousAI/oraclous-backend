"""OHM loader (ORAA-4 §21 domain layer; OHM v1.0 spec §3/§7).

Slice-1 load: parse YAML (or accept an already-parsed object), validate the structured schema, gate
the version, and cross-check that the runtime entrypoint names a declared capability binding. Atomic
*reference* resolution (capabilities/models against the registry), signature verification, and
governance are layered on in slices 2-3 — this function is the seam they extend, not replace.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import ValidationError

from oraclous_harness_runtime_service.domain.ohm.errors import (
    OHMParseError,
    OHMSchemaError,
    OHMVersionError,
)
from oraclous_harness_runtime_service.domain.ohm.manifest import OHMManifest

_SUPPORTED_VERSIONS = frozenset({"1.0"})


def load_ohm(raw: str | dict[str, Any]) -> OHMManifest:
    """Parse + validate an OHM document into an ``OHMManifest`` (fail-closed)."""
    if isinstance(raw, str):
        try:
            document = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise OHMParseError(f"OHM YAML is malformed: {exc}") from exc
    else:
        document = raw

    if not isinstance(document, dict):
        raise OHMParseError("OHM document must be a mapping at the top level")

    version = document.get("ohm_version")
    if version not in _SUPPORTED_VERSIONS:
        raise OHMVersionError(
            f"unsupported ohm_version {version!r}; supported: {sorted(_SUPPORTED_VERSIONS)}"
        )

    try:
        manifest = OHMManifest.model_validate(document)
    except ValidationError as exc:
        raise OHMSchemaError(f"OHM failed schema validation: {exc}") from exc

    # entrypoint must name a declared capability binding (structural cross-check).
    if manifest.entrypoint_capability() is None:
        raise OHMSchemaError(
            f"runtime.entrypoint {manifest.runtime.entrypoint!r} does not match any "
            "capabilities[].binding"
        )
    return manifest
