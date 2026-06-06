"""Governance policy sets + enforcement (ORAA-4 §21 domain layer; Section 6 + Structured Governance
Taxonomy v1.0).

An OHM references a policy set via ``governance.policy_set_ref``; this module is the built-in
catalogue of those sets (Taxonomy §2) and the coded enforcement of their *load-time* constraints:
signature requirement, capability allocation (allowed registries + forbidden capabilities), and BYOM
limits (allowed providers / protocol shapes). The *runtime* ceilings (tool-call + wall-time budgets,
HITL gates, redaction) are carried in ``PolicyEnvelope`` and enforced inside the tool-use loop.

The governing rule (Section 6): **a prose instruction never overrides a structured policy — code
wins.** Every check here is deterministic and fail-closed; a violation is an ``OHMGovernanceError``.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from oraclous_harness_runtime_service.domain.ohm.errors import OHMGovernanceError
from oraclous_harness_runtime_service.domain.ohm.manifest import OHMManifest


@dataclass(frozen=True, slots=True)
class PolicySet:
    """A named, versioned bundle of governance constraints (Taxonomy §1)."""

    id: str
    require_signature: bool = False
    max_tokens: int | None = None
    max_wall_time_seconds: int | None = None
    max_tool_calls: int | None = None
    allowed_providers: tuple[str, ...] | None = None  # None → any provider
    allowed_protocol_shapes: tuple[str, ...] | None = None  # None → any shape
    allowed_registries: tuple[str, ...] = ("core", "org:*")
    forbidden_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyEnvelope:
    """The effective runtime ceilings the tool-use loop enforces for one execution."""

    max_iterations: int
    max_tool_calls: int | None
    max_wall_time_seconds: int | None
    max_tokens: int | None
    gated_bindings: frozenset[str] = field(default_factory=frozenset)
    redact_patterns: tuple[str, ...] = ()


# Built-in catalogue (Structured Governance Taxonomy v1.0 §2). The single source until a policy
# service resolves them dynamically.
POLICY_SETS: dict[str, PolicySet] = {
    "policy-set:development-default@1.0.0": PolicySet(
        id="policy-set:development-default@1.0.0",
        require_signature=False,
        max_tokens=200_000,
        max_wall_time_seconds=600,
        max_tool_calls=200,
        allowed_registries=("core", "org:*"),
    ),
    "policy-set:staging-default@1.0.0": PolicySet(
        id="policy-set:staging-default@1.0.0",
        require_signature=True,
        max_tokens=100_000,
        max_wall_time_seconds=300,
        max_tool_calls=100,
        allowed_registries=("core", "org:*"),
    ),
    "policy-set:production-default@1.0.0": PolicySet(
        id="policy-set:production-default@1.0.0",
        require_signature=True,
        max_tokens=50_000,
        max_wall_time_seconds=180,
        max_tool_calls=50,
        allowed_registries=("core", "org:*"),
    ),
    "policy-set:production-strict@1.0.0": PolicySet(
        id="policy-set:production-strict@1.0.0",
        require_signature=True,
        max_tokens=20_000,
        max_wall_time_seconds=60,
        max_tool_calls=20,
        allowed_providers=("anthropic",),
        allowed_protocol_shapes=("native",),
        allowed_registries=("core",),
        forbidden_capabilities=("core/shell-exec@*", "core/arbitrary-http@*"),
    ),
    "policy-set:production-federated@1.0.0": PolicySet(
        id="policy-set:production-federated@1.0.0",
        require_signature=True,
        max_tokens=50_000,
        max_wall_time_seconds=180,
        max_tool_calls=50,
        allowed_registries=("core", "org:*", "federated:*"),
    ),
}

DEFAULT_POLICY_SET_REF = "policy-set:development-default@1.0.0"


def resolve_policy_set(ref: str | None) -> PolicySet:
    """Resolve ``governance.policy_set_ref`` to a known policy set (fail-closed on unknown)."""
    if not ref:
        return POLICY_SETS[DEFAULT_POLICY_SET_REF]
    policy = POLICY_SETS.get(ref)
    if policy is None:
        raise OHMGovernanceError(f"unknown policy_set_ref {ref!r}")
    return policy


def _registry_of(ref: str) -> str:
    """``core/echo@1`` → ``core``; ``org:<id>/x@1`` → ``org:<id>``."""
    head = ref.split("/", 1)[0]
    return head


def _registry_allowed(registry: str, allowed: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(registry, pattern) for pattern in allowed)


def enforce_load_policy(manifest: OHMManifest, policy: PolicySet) -> None:
    """Coded load-time governance: capability allocation + BYOM limits (fail-closed)."""
    for cap in manifest.capabilities:
        registry = _registry_of(cap.ref)
        if not _registry_allowed(registry, policy.allowed_registries):
            raise OHMGovernanceError(
                f"capability {cap.ref!r} is in registry {registry!r}, not allowed by {policy.id}"
            )
        for pattern in policy.forbidden_capabilities:
            if fnmatch.fnmatch(cap.ref, pattern):
                raise OHMGovernanceError(f"capability {cap.ref!r} is forbidden by {policy.id}")
    for model in manifest.models:
        provider = model.binding.split("/", 1)[0]
        if policy.allowed_providers is not None and provider not in policy.allowed_providers:
            raise OHMGovernanceError(f"model provider {provider!r} is not allowed by {policy.id}")
        if (
            policy.allowed_protocol_shapes is not None
            and model.protocol_shape not in policy.allowed_protocol_shapes
        ):
            raise OHMGovernanceError(
                f"protocol_shape {model.protocol_shape!r} is not allowed by {policy.id}"
            )


def build_envelope(
    manifest: OHMManifest, policy: PolicySet, *, hard_max_iterations: int
) -> PolicyEnvelope:
    """Build the effective runtime envelope: the stricter of the service cap and the policy budget,
    plus HITL gates (capabilities flagged ``config.hitl: true``) and OHM redaction patterns."""
    gated = frozenset(c.binding for c in manifest.capabilities if c.config.get("hitl") is True)
    redact = tuple(str(p) for p in (manifest.governance.redact_patterns or []))
    # iteration cap: the loop's own guard, bounded by the service hard cap (and tool-call budget).
    max_iterations = hard_max_iterations
    return PolicyEnvelope(
        max_iterations=max_iterations,
        max_tool_calls=policy.max_tool_calls,
        max_wall_time_seconds=policy.max_wall_time_seconds,
        max_tokens=policy.max_tokens,
        gated_bindings=gated,
        redact_patterns=redact,
    )
