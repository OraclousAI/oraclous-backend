"""OHM loader (domain layer; OHM v1.0 spec §3/§7).

Slice-1 load: parse YAML (or accept an already-parsed object), validate the structured schema, gate
the version, and cross-check that the runtime entrypoint names a declared capability binding. Atomic
*reference* resolution (capabilities/models against the registry), signature verification, and
governance are layered on in slices 2-3 — this function is the seam they extend, not replace.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import ValidationError

from oraclous_ohm.errors import (
    OHMDagError,
    OHMParseError,
    OHMSchemaError,
    OHMVersionError,
)
from oraclous_ohm.manifest import OHMManifest

_SUPPORTED_VERSIONS = frozenset({"1.0", "1.1"})


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

    # entrypoint cross-check: a v1.1 team names a member role (or a capability binding); with actors
    # declared it names an actor role; otherwise a capability binding (the single-agent case).
    entrypoint = manifest.runtime.entrypoint
    if manifest.members:
        if manifest.member_by_role(entrypoint) is None and manifest.entrypoint_capability() is None:
            raise OHMSchemaError(
                f"runtime.entrypoint {entrypoint!r} matches no members[].role or "
                "capabilities[].binding"
            )
    elif manifest.actors:
        if manifest.entrypoint_actor() is None:
            raise OHMSchemaError(f"runtime.entrypoint {entrypoint!r} matches no actors[].role")
    elif manifest.entrypoint_capability() is None:
        raise OHMSchemaError(
            f"runtime.entrypoint {entrypoint!r} does not match any capabilities[].binding"
        )

    # Bindings + roles are dispatch keys downstream — duplicates would silently shadow each other
    # (wrong instance dispatched / dropped tools), so reject them at load time.
    _reject_duplicates([c.binding for c in manifest.capabilities], "capabilities[].binding")
    _reject_duplicates([m.role for m in manifest.models], "models[].role")
    _reject_duplicates([p.role for p in manifest.prompts], "prompts[].role")
    _reject_duplicates([a.role for a in manifest.actors], "actors[].role")

    # a v1.1 team's member DAG must be acyclic, reference only declared members, and carry no
    # duplicate roles — reject a malformed topology at load (fail-closed) rather than at run time.
    if manifest.members:
        from oraclous_ohm.dag import topological_stages

        try:
            topological_stages(manifest.members)
        except OHMDagError as exc:
            raise OHMSchemaError(f"invalid team member DAG: {exc}") from exc
    return manifest


def _reject_duplicates(values: list[str], field: str) -> None:
    seen: set[str] = set()
    for v in values:
        if v in seen:
            raise OHMSchemaError(f"duplicate {field}: {v!r}")
        seen.add(v)
