"""#596 (ADR-047 §5) — the seed defaults: a fresh org surveys non-empty, the policy template is a
known-ref + the re-baselined 3-layer budget (L1<=L2, no OHMMemberBudget), ≥3 acyclic reference
topologies, bootstrap is idempotent, diff-accept never clobbers a user edit; a seeded team
passes the shared validator.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_ohm.dag import topological_stages
from oraclous_ohm.import_ import assemble_and_report
from oraclous_ohm.manifest import (
    OHMManifest,
    OHMMember,
    OHMMetadata,
    OHMRuntime,
    resolve_member_caps,
)
from oraclous_ohm.parse import load_ohm
from oraclous_ohm.seeds import (
    DEFAULT_POLICY_SET_REF,
    bootstrap_seed,
    default_seed_set,
    seed_capability_inventory,
    seed_policy_template,
    seed_reference_topologies,
    survey_catalog,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")

# the real registered capability slugs (capability-registry builtin.py) — the inventory must only
# reference these (no phantom tool).
_REAL = {
    "web-research",
    "websearch",
    "webfetch",
    "knowledge-retriever",
    "find-similar",
    "recall-memory",
    "read",
    "write",
    "edit",
    "grep",
    "glob",
    "bash",
    "send-to-drafts",
    "text-tools",
    "rest-connector",
}


def test_a_fresh_org_survey_is_non_empty() -> None:
    # a fresh org's LIVE registry survey is sparse/empty → the seed inventory makes it non-empty
    catalog = survey_catalog(seed_capability_inventory(), registered=[])
    assert catalog
    assert "web-research" in catalog and "send-to-drafts" in catalog


def test_survey_unions_the_seed_inventory_with_the_live_registry() -> None:
    catalog = survey_catalog(seed_capability_inventory(), registered=["core/some-live-tool@1.0.0"])
    assert "web-research" in catalog  # seed
    assert "some-live-tool" in catalog  # live, normalised to its slug


def test_every_seed_tool_is_a_real_registered_capability() -> None:
    inv = seed_capability_inventory()
    for a in inv.archetypes:
        for t in a.tools:
            assert t in _REAL, f"archetype {a.name!r} references a phantom tool {t!r}"
    for g in inv.tool_groups:
        for t in g.tools:
            assert t in _REAL, f"tool-group {g.name!r} references a phantom tool {t!r}"


def test_the_policy_template_is_a_known_ref_and_a_3_layer_budget() -> None:
    p = seed_policy_template()
    assert (
        p.governance.policy_set_ref == DEFAULT_POLICY_SET_REF
    )  # resolve_policy_set will accept it
    assert p.governance.redact_patterns  # bounded set
    b = p.budget
    assert b.max_tokens_total and b.max_tool_calls_total  # L2 pool
    # L1 per-agent defaults each AUTHORED <= the L2 pool (the resolve_member_caps clamp invariant)
    assert b.max_tokens_per_member is not None and b.max_tokens_per_member <= b.max_tokens_total
    assert (
        b.max_tool_calls_per_member is not None
        and b.max_tool_calls_per_member <= b.max_tool_calls_total
    )


def test_resolve_member_caps_clamps_each_default_cap_to_the_pool() -> None:
    # the deterministic guardrail the compiler reviewer asserts: a member with no own caps inherits
    # the seed per-agent default, clamped <= the pool.
    b = seed_policy_template().budget
    m = OHMMember(role="x", kind="agent", manifest_ref="org:x/x@1")
    tokens, calls = resolve_member_caps(m, b)
    assert tokens is not None and tokens <= b.max_tokens_total
    assert calls is not None and calls <= b.max_tool_calls_total


def test_at_least_three_reference_topologies_each_acyclic_and_valid() -> None:
    topos = seed_reference_topologies()
    assert len(topos) >= 3
    names = {t.name for t in topos}
    assert {"fan-out-fan-in", "standing-team", "gated-pipeline"} <= names
    for t in topos:
        stages = topological_stages(t.members)  # raises OHMDagError on a cycle / unknown dep
        assert stages, f"topology {t.name!r} produced no stages"


def test_a_seed_governed_team_passes_the_shared_validator() -> None:
    # a team carrying the seed governance + budget + a seed topology is GO-ready (would_block False)
    topo = {t.name: t for t in seed_reference_topologies()}["gated-pipeline"]
    p = seed_policy_template()
    manifest = OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(
            id=uuid.uuid4(), name="seeded", owner_organization_id=_ORG, kind="team"
        ),
        members=list(topo.members),
        governance=p.governance,
        budget=p.budget,
        runtime=OHMRuntime(entrypoint="drafter"),
    )
    loaded = load_ohm(manifest.model_dump(mode="json"))  # schema-valid OHM v1.1
    result = assemble_and_report(
        "seeded", list(loaded.members), owner_organization_id=_ORG, shape="compiled"
    )
    assert result.report.would_block is False  # GO-ready, not BLOCKED


def test_bootstrap_first_run_then_idempotent() -> None:
    s = default_seed_set()
    merged1, diff1 = bootstrap_seed(None, s)  # first run → everything added
    assert diff1.added and not diff1.user_modified
    merged2, diff2 = bootstrap_seed(merged1, s)  # re-seed the SAME set
    assert not diff2.added and not diff2.changed and not diff2.user_modified  # idempotent


def test_diff_accept_never_clobbers_a_user_edited_seed() -> None:
    s = default_seed_set()
    edited = s.model_copy(deep=True)
    edited.inventory.archetypes[0].tools = ["web-research"]  # the user edited the researcher
    merged, diff = bootstrap_seed(edited, s)  # re-seed the default over the user's org
    assert "researcher" in diff.user_modified  # flagged as user-modified
    kept = {a.name: a for a in merged.inventory.archetypes}["researcher"]
    assert kept.tools == ["web-research"]  # the user's edit is KEPT, never overwritten


def test_diff_accept_adds_a_new_default_seed_without_touching_existing() -> None:
    s = default_seed_set()
    # an org that pre-dates a new archetype: drop one from "existing", re-seed the full default
    existing = s.model_copy(deep=True)
    dropped = existing.inventory.archetypes.pop().name
    merged, diff = bootstrap_seed(existing, s)
    assert dropped in diff.added  # the new default archetype is added
    assert not diff.user_modified  # nothing the user edited


def test_slug_does_not_collapse_a_foreign_namespace() -> None:
    # CTO security note: seeds._slug must align with compiler.validate._tool_slug (#594) — a foreign
    # namespace must NOT collapse to a bare surveyed slug.
    from oraclous_ohm.seeds import _slug

    assert _slug("Web Research") == "web-research"
    assert _slug("core/web-research@1.0.0") == "web-research"
    assert _slug("evil/web-research") != "web-research"  # no namespace collapse (masquerade closed)
    assert _slug("😈/web-research") != "web-research"
