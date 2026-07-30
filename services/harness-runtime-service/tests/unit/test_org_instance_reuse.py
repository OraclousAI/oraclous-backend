"""Unit: a run uses the org's configured tool instance, never a freshly minted empty copy (#663).

``_materialise`` keyed find-or-create ONLY by the deterministic per-harness name
(``harness:<id>:<binding>``). Team-run sub-harness ids are unique per import, so a compiled member
never matched a prior instance and always minted a fresh one — unbound to the org's credential,
stuck in ``CONFIGURATION_REQUIRED``, and every dispatch 409'd (team run ``9ddf00f3``: both
``github-reader`` and ``github-sink``, 8 attempts, ~33k real tokens, zero successful calls) while
the org's own configured instance of the same capability sat unused.

The #663 contract pinned here:

* a configured org instance of the declared capability is REUSED, not shadowed by a mint;
* a keyed capability with no configured source fails BEFORE the LLM is ever built (no model
  tokens), naming the capability binding and the missing credential type — and mints no junk
  instance (the registry has no instance-delete to compensate with);
* an unconfigured sibling (e.g. the junk minted by a pre-fix run) is never selected as the source;
* an ``oauth_token``-only capability is exempt: the broker resolves OAuth at execute time from
  (org, user, provider, scopes) and ignores ``credential_mappings``, so it must keep creating and
  running exactly as today (Google Drive et al. must not start fail-fasting);
* manifest-authored ``credential_mappings`` still bind at creation (the pre-#663 working path).

"Configured" means every required non-OAuth credential type has a mapping — computed from
``required_credentials`` vs ``credential_mappings`` on the listed instance, NOT from ``status``
alone, because a previously executed instance sits in ``SUCCESS``/``FAILED`` (never back to
``READY``) yet is exactly the proven-working instance the run should use.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_harness_runtime_service.services.harness_execution_service import (
    HarnessExecutionError,
    HarnessExecutionService,
)
from oraclous_ohm.manifest import OHMCapability, OHMManifest, OHMMetadata, OHMRuntime
from oraclous_ohm.signatures import TrustStore

pytestmark = pytest.mark.unit

_ORG = uuid.uuid4()

_KEYED_DESCRIPTOR = {
    "id": "cap-gh",
    "metadata": {"name": "GitHub Reader"},
    "spec": {
        "capabilities": [],
        "credential_requirements": [{"type": "api_key", "provider": "github", "required": True}],
    },
}

_OAUTH_DESCRIPTOR = {
    "id": "cap-drive",
    "metadata": {"name": "Google Drive Reader"},
    "spec": {
        "capabilities": [],
        "credential_requirements": [
            {
                "type": "oauth_token",
                "provider": "google",
                "required": True,
                "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
            }
        ],
    },
}


class _Registry:
    """A registry fake: canned ``list_instances`` rows + recording create/configure calls."""

    def __init__(self, instances: list[dict[str, Any]] | None = None) -> None:
        self.instances = list(instances or [])
        self.created: list[dict[str, Any]] = []
        self.configured: list[tuple[uuid.UUID, dict[str, str]]] = []

    async def list_instances(self) -> list[dict[str, Any]]:
        return list(self.instances)

    async def create_instance(
        self, *, capability_id: str, name: str, configuration: dict[str, Any]
    ) -> dict[str, Any]:
        self.created.append(
            {"capability_id": capability_id, "name": name, "configuration": configuration}
        )
        return {"id": str(uuid.uuid4())}

    async def configure_credentials(
        self, instance_id: uuid.UUID, mappings: dict[str, str]
    ) -> dict[str, Any]:
        self.configured.append((instance_id, mappings))
        return {}


def _service(registry: _Registry) -> HarnessExecutionService:
    return HarnessExecutionService(
        registry=registry,
        broker=None,
        executions=None,
        assignments=None,
        checkpoints=None,
        provenance=None,
        trust=TrustStore({}),
        require_signature=False,
        force_policy_set=None,
        llm_mode="fake",
        llm_base_urls={},
        llm_timeout=1.0,
        llm_allow_private=True,
        max_iterations=6,
        memory=None,
    )


def _manifest(
    ref: str = "core/github-reader@1.0.0",
    binding: str = "github-reader",
    config: dict[str, Any] | None = None,
) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="m", owner_organization_id=_ORG, kind="agent"),
        capabilities=[OHMCapability(ref=ref, binding=binding, config=config or {})],
        runtime=OHMRuntime(entrypoint=binding),
    )


def _row(
    *,
    instance_id: str,
    name: str,
    capability_id: str = "cap-gh",
    status: str = "READY",
    required: list[str] | None = None,
    mappings: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "id": instance_id,
        "name": name,
        "capability_id": capability_id,
        "status": status,
        "required_credentials": ["api_key"] if required is None else required,
        "credential_mappings": {"api_key": "cred-1"} if mappings is None else mappings,
    }


_RESOLVED_KEYED = {"github-reader": {"id": "cap-gh", "descriptor": _KEYED_DESCRIPTOR}}
_RESOLVED_OAUTH = {"drive": {"id": "cap-drive", "descriptor": _OAUTH_DESCRIPTOR}}


async def test_the_orgs_configured_instance_is_reused_instead_of_minting() -> None:
    """Acceptance 1: the run binds the org's READY instance of the declared capability — it does
    NOT create a fresh (credential-less) copy alongside it."""
    ready_id = str(uuid.uuid4())
    registry = _Registry([_row(instance_id=ready_id, name="github-reader")])
    instance_by_binding, _ = await _service(registry)._materialise(_manifest(), _RESOLVED_KEYED)
    assert instance_by_binding["github-reader"] == uuid.UUID(ready_id)
    assert registry.created == []  # nothing minted — the org's instance IS the run's instance


async def test_a_previously_executed_instance_still_counts_as_configured() -> None:
    """The registry parks an instance in SUCCESS/FAILED after a dispatch (never back to READY).
    Configured-ness comes from its mappings covering the required types, not the status label —
    else the proven-working instance would be skipped the day after it worked."""
    used_id = str(uuid.uuid4())
    registry = _Registry([_row(instance_id=used_id, name="github-reader", status="SUCCESS")])
    instance_by_binding, _ = await _service(registry)._materialise(_manifest(), _RESOLVED_KEYED)
    assert instance_by_binding["github-reader"] == uuid.UUID(used_id)
    assert registry.created == []


async def test_missing_credential_fails_fast_naming_capability_and_type() -> None:
    """Acceptance 3 (fail-closed, §3.5): no configured instance + no manifest mappings for a keyed
    capability → the run fails at setup, naming the binding and the missing credential type, and
    mints NO junk instance (the registry cannot delete one)."""
    registry = _Registry([])
    with pytest.raises(HarnessExecutionError) as err:
        await _service(registry)._materialise(_manifest(), _RESOLVED_KEYED)
    message = str(err.value)
    assert "github-reader" in message  # names the capability the user must connect
    assert "api_key" in message  # names what is missing
    assert registry.created == []  # fail-fast means no unconfigurable instance is minted


async def test_an_unconfigured_sibling_is_never_the_source() -> None:
    """The junk instances minted by pre-fix runs (CONFIGURATION_REQUIRED, empty mappings) must not
    satisfy the reuse lookup — else the fix would re-select the very instance that 409s."""
    registry = _Registry(
        [
            _row(
                instance_id=str(uuid.uuid4()),
                name="harness:00000000-0000-0000-0000-000000000001:github-reader",
                status="CONFIGURATION_REQUIRED",
                mappings={},
            )
        ]
    )
    with pytest.raises(HarnessExecutionError):
        await _service(registry)._materialise(_manifest(), _RESOLVED_KEYED)
    assert registry.created == []


async def test_oauth_only_capability_is_exempt_from_fail_fast() -> None:
    """The broker resolves ``oauth_token`` at execute time from (org, user, provider, scopes) and
    ignores ``credential_mappings`` entirely — so an OAuth-only capability with no org instance
    must keep creating + running exactly as today, never fail-fast."""
    registry = _Registry([])
    instance_by_binding, _ = await _service(registry)._materialise(
        _manifest(ref="core/google-drive-reader@1.0.0", binding="drive"), _RESOLVED_OAUTH
    )
    assert "drive" in instance_by_binding
    assert len(registry.created) == 1  # created as before; the broker handles OAuth at dispatch


async def test_manifest_mappings_still_bind_at_creation() -> None:
    """Acceptance 2 (the pre-#663 working path, pinned): with no org instance but manifest-authored
    ``credential_mappings`` covering the required types, a new instance is created AND bound before
    its first dispatch."""
    registry = _Registry([])
    manifest = _manifest(config={"credential_mappings": {"api_key": "cred-9"}})
    instance_by_binding, _ = await _service(registry)._materialise(manifest, _RESOLVED_KEYED)
    assert "github-reader" in instance_by_binding
    assert len(registry.created) == 1
    assert registry.configured == [(instance_by_binding["github-reader"], {"api_key": "cred-9"})]


async def test_fail_fast_happens_before_the_llm_is_built() -> None:
    """Acceptance 5 (no model tokens): the credential failure must surface from capability setup,
    BEFORE ``_build_runnable`` ever constructs the LLM client — the 5,800-token run in #663 spent
    real money on calls that could never succeed."""
    from oraclous_harness_runtime_service.domain.policy import resolve_policy_set

    registry = _Registry([])
    service = _service(registry)
    built: list[str] = []

    async def _recording_build_llm(manifest: Any, org_id: Any) -> Any:  # noqa: ANN401
        built.append("llm")
        raise AssertionError("the LLM must never be built when credentials are missing")

    service._build_llm = _recording_build_llm  # type: ignore[method-assign]
    service._resolve_all = lambda manifest: _async_return(_RESOLVED_KEYED)  # type: ignore[method-assign]
    with pytest.raises(HarnessExecutionError):
        await service._build_runnable(_manifest(), resolve_policy_set(None), _ORG)
    assert built == []  # zero tokens: the failure fired at capability setup, not mid-loop


def _async_return(value: Any) -> Any:  # noqa: ANN401
    async def _coro() -> Any:  # noqa: ANN401
        return value

    return _coro()
