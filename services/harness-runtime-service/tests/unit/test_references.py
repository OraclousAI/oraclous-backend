"""OHM atomic reference resolution (slice 2): all capabilities resolve, or the whole load fails."""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.ohm.errors import OHMReferenceError
from oraclous_harness_runtime_service.domain.ohm.parse import load_ohm
from oraclous_harness_runtime_service.domain.ohm.references import resolve_capabilities

pytestmark = pytest.mark.unit

_OHM = {
    "ohm_version": "1.0",
    "metadata": {
        "id": "01976e3a-7c9b-7b00-9c45-1234567890ab",
        "name": "Two-tool agent",
        "owner_organization_id": "01976e3a-0000-7000-9c45-000000000000",
    },
    "capabilities": [
        {"ref": "core/postgresql-reader@1.0.0", "binding": "pg"},
        {"ref": "core/github-reader@1.0.0", "binding": "gh"},
    ],
    "models": [
        {"role": "primary", "binding": "anthropic/claude-opus-4-8", "protocol_shape": "native"}
    ],
    "prompts": [{"role": "primary", "source": "inline", "body": "Use the tools."}],
    "runtime": {"entrypoint": "pg"},
}


async def test_resolves_every_capability() -> None:
    manifest = load_ohm(_OHM)

    async def resolve(ref: str, explicit_id: str | None) -> dict:
        return {"id": f"id-for-{ref}", "name": ref, "descriptor": {"spec": {"capabilities": []}}}

    resolved = await resolve_capabilities(manifest, resolve)
    assert set(resolved) == {"pg", "gh"}
    assert resolved["gh"]["id"] == "id-for-core/github-reader@1.0.0"


async def test_one_bad_ref_fails_the_whole_load() -> None:
    manifest = load_ohm(_OHM)

    async def resolve(ref: str, explicit_id: str | None) -> dict:
        if "github" in ref:
            raise RuntimeError("not found in registry")
        return {"id": "ok", "descriptor": {}}

    with pytest.raises(OHMReferenceError):
        await resolve_capabilities(manifest, resolve)
