"""Governance policy sets (slice 3): resolution, coded load-time enforcement, runtime envelope."""

from __future__ import annotations

import pytest
from oraclous_harness_runtime_service.domain.policy import (
    DEFAULT_POLICY_SET_REF,
    build_envelope,
    enforce_load_policy,
    resolve_policy_set,
)
from oraclous_ohm.errors import OHMGovernanceError
from oraclous_ohm.parse import load_ohm

pytestmark = pytest.mark.unit


def _ohm(
    *,
    cap_ref: str = "core/postgresql-reader@1.0.0",
    provider: str = "anthropic",
    shape: str = "native",
    policy_ref: str | None = None,
    hitl: bool = False,
    redact: list[str] | None = None,
):
    doc = {
        "ohm_version": "1.0",
        "metadata": {
            "id": "01976e3a-7c9b-7b00-9c45-1234567890ab",
            "name": "T",
            "owner_organization_id": "01976e3a-0000-7000-9c45-000000000000",
        },
        "capabilities": [{"ref": cap_ref, "binding": "pg", "config": {"hitl": hitl}}],
        "models": [{"role": "primary", "binding": f"{provider}/m", "protocol_shape": shape}],
        "prompts": [{"role": "primary", "source": "inline", "body": "go"}],
        "governance": {"policy_set_ref": policy_ref, "redact_patterns": redact or []},
        "runtime": {"entrypoint": "pg"},
    }
    return load_ohm(doc)


def test_resolve_known_default_and_unknown() -> None:
    assert resolve_policy_set("policy-set:production-strict@1.0.0").id.endswith(
        "production-strict@1.0.0"
    )
    assert resolve_policy_set(None).id == DEFAULT_POLICY_SET_REF
    with pytest.raises(OHMGovernanceError):
        resolve_policy_set("policy-set:does-not-exist@9.9.9")


def test_development_default_allows_a_normal_harness() -> None:
    enforce_load_policy(_ohm(), resolve_policy_set(None))  # no raise


def test_strict_forbids_a_forbidden_capability() -> None:
    strict = resolve_policy_set("policy-set:production-strict@1.0.0")
    with pytest.raises(OHMGovernanceError):
        enforce_load_policy(_ohm(cap_ref="core/shell-exec@1.0.0"), strict)


def test_strict_rejects_a_disallowed_registry() -> None:
    strict = resolve_policy_set("policy-set:production-strict@1.0.0")  # registries: core only
    with pytest.raises(OHMGovernanceError):
        enforce_load_policy(_ohm(cap_ref="org:abc/custom@1.0.0"), strict)


def test_strict_rejects_a_disallowed_provider() -> None:
    strict = resolve_policy_set("policy-set:production-strict@1.0.0")  # providers: anthropic only
    with pytest.raises(OHMGovernanceError):
        enforce_load_policy(_ohm(provider="openai"), strict)


def test_strict_rejects_a_disallowed_protocol_shape() -> None:
    strict = resolve_policy_set("policy-set:production-strict@1.0.0")  # shapes: native only
    with pytest.raises(OHMGovernanceError):
        enforce_load_policy(_ohm(shape="openai-compatible"), strict)


def test_envelope_carries_budget_gates_and_redaction() -> None:
    strict = resolve_policy_set("policy-set:production-strict@1.0.0")
    env = build_envelope(_ohm(hitl=True, redact=["secret-\\d+"]), strict, hard_max_iterations=9)
    assert env.max_tool_calls == 20  # from the policy set
    assert env.max_wall_time_seconds == 60
    assert env.max_iterations == 9  # service hard cap
    assert env.gated_bindings == frozenset({"pg"})  # config.hitl: true
    assert env.tool_ceiling == frozenset({"pg"})  # the declared capability binding(s)
    assert env.redact_patterns == ("secret-\\d+",)


def test_envelope_ceiling_is_the_declared_capability_set_distinct_from_gating() -> None:
    # ADR-035 §5: the ceiling is every declared binding (the closed dispatchable set), independent
    # of HITL gating. With no HITL flag, the binding is in the ceiling but NOT gated — distinct.
    strict = resolve_policy_set("policy-set:production-strict@1.0.0")
    env = build_envelope(_ohm(hitl=False), strict, hard_max_iterations=9)
    assert env.tool_ceiling == frozenset(
        {"pg"}
    )  # the agent may only ever dispatch its declared tools
    assert env.gated_bindings == frozenset()  # not HITL-flagged — ceiling != gating


def test_external_ceiling_intersects_the_declared_set_fail_closed() -> None:
    # Red-team G-A: a caller-supplied ceiling (a team member's tools[]) caps the runtime ceiling by
    # INTERSECTION, so a manifest (even one fetched by manifest_ref) can never exceed it.
    strict = resolve_policy_set("policy-set:production-strict@1.0.0")
    manifest = _ohm()  # declares one binding: 'pg'

    # member allows 'pg' -> the binding survives the intersection
    assert build_envelope(
        manifest, strict, hard_max_iterations=9, external_ceiling=frozenset({"pg"})
    ).tool_ceiling == frozenset({"pg"})

    # member's tools[] does NOT include 'pg' -> the manifest's binding is DENIED (capped out)
    assert (
        build_envelope(
            manifest, strict, hard_max_iterations=9, external_ceiling=frozenset({"other"})
        ).tool_ceiling
        == frozenset()
    )

    # an empty member ceiling -> deny-all (deny-by-default, ADR-032)
    assert (
        build_envelope(
            manifest, strict, hard_max_iterations=9, external_ceiling=frozenset()
        ).tool_ceiling
        == frozenset()
    )

    # no external cap (single-agent path) -> the declared set is unchanged
    assert build_envelope(
        manifest, strict, hard_max_iterations=9, external_ceiling=None
    ).tool_ceiling == frozenset({"pg"})


def test_forbidden_matches_an_unversioned_ref() -> None:
    # H2: an unversioned/odd-cased ref must not dodge a "core/shell-exec@*" forbidden glob.
    strict = resolve_policy_set("policy-set:production-strict@1.0.0")
    with pytest.raises(OHMGovernanceError):
        enforce_load_policy(_ohm(cap_ref="core/shell-exec"), strict)
    with pytest.raises(OHMGovernanceError):
        enforce_load_policy(_ohm(cap_ref="core/Shell-Exec@2.0.0"), strict)


def test_tool_call_budget_binds_within_the_iteration_cap() -> None:
    # M2: the per-tier tool-call budget shapes the iteration cap (so tiers actually differ).
    strict = resolve_policy_set("policy-set:production-strict@1.0.0")  # max_tool_calls=20
    env = build_envelope(_ohm(), strict, hard_max_iterations=25)
    assert env.max_iterations == 21  # min(25, 20 + 1)


def test_bad_redact_pattern_is_a_governance_error() -> None:
    # M4: a malformed author-supplied regex is a clean 422, not a 500.
    with pytest.raises(OHMGovernanceError):
        build_envelope(_ohm(redact=["("]), resolve_policy_set(None), hard_max_iterations=25)


def test_too_many_redact_patterns_rejected() -> None:
    with pytest.raises(OHMGovernanceError):
        build_envelope(
            _ohm(redact=[f"p{i}" for i in range(30)]),
            resolve_policy_set(None),
            hard_max_iterations=25,
        )


# ── #698 D4: the imported-MCP ref shape must pass the default policy set ──────────────────────────
#
# ``_registry_of`` takes everything before the FIRST "/" as the registry name. The documented ref
# for an imported tool is ``org:<org-id>/<label>-<tool_name>``, so the registry reads as
# ``org:<org-id>`` and matches the ``org:*`` glob that development-default allows. A ref that kept
# the label as its own path segment (``org:<id>/<label>/<tool>``) reads the same registry — but it
# is the shape that fails RESOLUTION (see test_registry_client.py), so both rules only agree on
# the hyphenated form. Shape confirmation is tracked as a Contract on #699.

_ORG_REF = "org:00000000-0000-0000-0000-0000000000aa/github-mcp-pull_request_read"


def test_the_documented_mcp_ref_passes_development_default() -> None:
    enforce_load_policy(_ohm(cap_ref=_ORG_REF), resolve_policy_set(None))  # no raise


def test_the_documented_mcp_ref_passes_with_a_version_suffix() -> None:
    enforce_load_policy(_ohm(cap_ref=f"{_ORG_REF}@1.0.0"), resolve_policy_set(None))  # no raise


def test_an_imported_tool_ref_without_the_org_registry_is_refused() -> None:
    """Fail-closed: a bare ``<label>-<tool>`` with no ``org:`` prefix reads its registry as the
    label itself, which no policy set allows — an imported tool cannot dodge tenancy scoping."""
    with pytest.raises(OHMGovernanceError):
        enforce_load_policy(_ohm(cap_ref="github-mcp-pull_request_read"), resolve_policy_set(None))


def test_the_documented_mcp_ref_is_still_refused_by_the_strict_set() -> None:
    """production-strict allows the ``core`` registry only — an org-imported tool stays out."""
    strict = resolve_policy_set("policy-set:production-strict@1.0.0")
    with pytest.raises(OHMGovernanceError):
        enforce_load_policy(_ohm(cap_ref=_ORG_REF), strict)


# ── #731: ``_registry_of`` must stay UN-hyphenated — repointing it would fail OPEN a gate ─────────
#
# ``_registry_of`` keeps everything before the FIRST "/" and does NOT hyphenate at all. #731's five
# collapsed copies never touch it: folding it into ``basic_slug`` would turn ``org:<uuid>`` into
# ``org-<uuid>``, which the ``allowed_registries`` glob ``"org:*"`` no longer matches — every
# org-imported capability would then be rejected by every policy set that allows ``org:*``, which is
# a fail-CLOSED break for a legitimate ref today but, read the other way, is exactly the shape of
# bug that would let a hyphen-disguised registry slip PAST a narrower allow-list glob tomorrow. This
# pins the un-hyphenated head as the deliberate, permanent shape.


@pytest.mark.security
def test_registry_of_head_is_not_hyphenated_so_the_allow_list_still_matches() -> None:
    import fnmatch

    from oraclous_harness_runtime_service.domain.policy import _registry_of

    registry = _registry_of("org:9f2c-11ee/x@1")
    assert registry == "org:9f2c-11ee"
    assert fnmatch.fnmatch(registry, "org:*")
