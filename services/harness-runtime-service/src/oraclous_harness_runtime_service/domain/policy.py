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
import re
from dataclasses import dataclass, field

from oraclous_ohm.errors import OHMGovernanceError
from oraclous_ohm.manifest import OHMManifest


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


# Redaction patterns are author-supplied — bound count/length and compile-validate to keep a
# malformed pattern a clean 422 (not a 500) and limit the catastrophic-backtracking (ReDoS) surface.
_MAX_REDACT_PATTERNS = 25
_MAX_REDACT_PATTERN_LEN = 200


def _registry_of(ref: str) -> str:
    """``core/echo@1`` → ``core``; ``org:<id>/x@1`` → ``org:<id>`` (lowercased)."""
    return ref.split("/", 1)[0].strip().lower()


def _name_part(value: str) -> str:
    """Drop the ``@version`` and lowercase — the version-/case-independent identity for matching."""
    return value.split("@", 1)[0].strip().lower()


def _registry_allowed(registry: str, allowed: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(registry, pattern.lower()) for pattern in allowed)


def _is_forbidden(ref: str, patterns: tuple[str, ...]) -> bool:
    name = _name_part(ref)
    return any(fnmatch.fnmatch(name, _name_part(pattern)) for pattern in patterns)


def _hitl_flagged(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def enforce_load_policy(manifest: OHMManifest, policy: PolicySet) -> None:
    """Coded load-time governance: capability allocation + BYOM limits (fail-closed). Matching is
    version- and case-independent so an unversioned/odd-cased ref can't dodge a forbidden glob."""
    for cap in manifest.capabilities:
        registry = _registry_of(cap.ref)
        if not _registry_allowed(registry, policy.allowed_registries):
            raise OHMGovernanceError(
                f"capability {cap.ref!r} is in registry {registry!r}, not allowed by {policy.id}"
            )
        if _is_forbidden(cap.ref, policy.forbidden_capabilities):
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


def _validated_redact_patterns(patterns: list[str]) -> tuple[str, ...]:
    if len(patterns) > _MAX_REDACT_PATTERNS:
        raise OHMGovernanceError(f"too many redact_patterns (max {_MAX_REDACT_PATTERNS})")
    out: list[str] = []
    for pattern in patterns:
        text = str(pattern)
        if len(text) > _MAX_REDACT_PATTERN_LEN:
            raise OHMGovernanceError("a redact pattern exceeds the maximum length")
        try:
            re.compile(text)
        except re.error as exc:
            raise OHMGovernanceError(f"invalid redact pattern {text!r}: {exc}") from exc
        out.append(text)
    return tuple(out)


def build_envelope(
    manifest: OHMManifest, policy: PolicySet, *, hard_max_iterations: int
) -> PolicyEnvelope:
    """Build the effective runtime envelope. The iteration cap is a safety backstop derived from the
    policy's tool-call budget (so a stricter tier's smaller budget actually binds), bounded by the
    service hard cap. HITL gates come from capabilities flagged ``config.hitl``; redaction patterns
    are validated (a bad pattern is a governance error, not a 500)."""
    gated = frozenset(
        c.binding for c in manifest.capabilities if _hitl_flagged(c.config.get("hitl"))
    )
    redact = _validated_redact_patterns(manifest.governance.redact_patterns or [])
    if policy.max_tool_calls is not None:
        max_iterations = min(hard_max_iterations, policy.max_tool_calls + 1)
    else:
        max_iterations = hard_max_iterations
    return PolicyEnvelope(
        max_iterations=max_iterations,
        max_tool_calls=policy.max_tool_calls,
        max_wall_time_seconds=policy.max_wall_time_seconds,
        max_tokens=policy.max_tokens,
        gated_bindings=gated,
        redact_patterns=redact,
    )
