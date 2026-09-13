"""#900 — ``resolved_schema`` on ``OHMCapability`` (ADR-053 decision 1).

ADR-053 rules that ``OHMCapability`` gains one new, optional, typed field::

    resolved_schema: dict[str, Any] | None = None

carrying a per-run, per-organisation JSON Schema for the capability's tool-call parameters,
computed by the compiler, which OVERRIDES the capability's stored registry schema for that one
run when present. Absent (``None``, the default) means resolution behaves exactly as it does
today. The ADR is explicit that the name is ``resolved_schema``, never ``parameters_schema`` (a
different field, with different semantics, already lives on the frontend's tool descriptor —
``oraclous-frontend``'s ``packages/api-client/src/tools.ts:35-37``).

RED-by-design until the ``[impl]`` lands: ``OHMCapability`` carries no ``resolved_schema`` field
today, and pydantic v2 with ``extra="ignore"`` silently DROPS an unknown constructor key rather
than raising — so, mirroring #730/#834's own documented rationale, every assertion here reads the
field's VALUE after construction/round-trip, never merely "construction did not raise" (which
would pass today and prove nothing).

The wire format and threading mechanics (how the runtime's tool-calling machinery actually
consumes this field) are #900's implementation, not this file's concern (ADR-053, "Scope not
decided here") — this file pins only the plain field contract: presence, optionality, and that a
capability built the OLD way (no ``resolved_schema`` argument, exactly how every existing call
site constructs one today) is unaffected.
"""

from __future__ import annotations

import pytest
from oraclous_ohm.manifest import OHMActor, OHMCapability, OHMManifest, OHMMetadata, OHMRuntime

pytestmark = pytest.mark.unit

_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
    "required": ["query"],
}


# ── presence + optionality ───────────────────────────────────────────────────────────────────


def test_capability_accepts_resolved_schema_and_returns_the_exact_dict() -> None:
    cap = OHMCapability(ref="core/web.search@1", binding="web.search", resolved_schema=_SCHEMA)
    assert cap.resolved_schema == _SCHEMA


def test_capability_resolved_schema_defaults_to_none_when_absent() -> None:
    cap = OHMCapability(ref="core/web.search@1", binding="web.search")
    assert cap.resolved_schema is None


def test_resolved_schema_defaults_to_none_explicitly_pinned_not_missing() -> None:
    # a bare "did not raise" would also pass if the attribute were simply absent — pin the
    # exact value, the way #834's own default test does.
    cap = OHMCapability(ref="core/web.search@1", binding="web.search")
    assert cap.resolved_schema is None
    assert hasattr(cap, "resolved_schema")


# ── round-trip through model_dump / model_validate ───────────────────────────────────────────


def test_resolved_schema_round_trips_through_model_dump_json() -> None:
    cap = OHMCapability(ref="core/web.search@1", binding="web.search", resolved_schema=_SCHEMA)
    dumped = cap.model_dump(mode="json")
    assert "resolved_schema" in dumped
    assert dumped["resolved_schema"] == _SCHEMA


def test_resolved_schema_survives_a_model_validate_round_trip() -> None:
    cap = OHMCapability(ref="core/web.search@1", binding="web.search", resolved_schema=_SCHEMA)
    reloaded = OHMCapability.model_validate(cap.model_dump(mode="json"))
    assert reloaded.resolved_schema == _SCHEMA


def test_resolved_schema_survives_a_full_manifest_round_trip() -> None:
    import uuid

    manifest = OHMManifest(
        ohm_version="1.0",
        metadata=OHMMetadata(id=uuid.uuid4(), name="s", owner_organization_id=uuid.uuid4()),
        capabilities=[
            OHMCapability(ref="core/web.search@1", binding="web.search", resolved_schema=_SCHEMA)
        ],
        actors=[OHMActor(role="primary", kind="agent")],
        runtime=OHMRuntime(entrypoint="primary"),
    )
    reloaded = OHMManifest.model_validate(manifest.model_dump(mode="json"))
    assert reloaded.capabilities[0].resolved_schema == _SCHEMA


# ── back-compat: a capability built the OLD way is unaffected ───────────────────────────────


def test_old_style_capability_construction_still_defaults_to_none() -> None:
    # exactly how every existing call site constructs a capability today (e.g. build_subharness
    # in packages/ohm/src/oraclous_ohm/import_/mapping.py) — no resolved_schema kwarg at all.
    cap = OHMCapability(ref="core/web.search@1", binding="web.search")
    assert cap.resolved_schema is None
    dumped_excluding_none = cap.model_dump(mode="json", exclude_none=True)
    assert "resolved_schema" not in dumped_excluding_none
    # the OLD three-key shape is exactly preserved when None fields are excluded from the dump
    assert dumped_excluding_none == {
        "ref": "core/web.search@1",
        "binding": "web.search",
        "config": {},
    }


def test_a_plain_model_dump_includes_the_key_as_none_when_unset() -> None:
    # NOT exclude_none — this is the dump call convention every current, non-compiler call site
    # actually uses (e.g. team_draft_service.py, assemble.py, compiler_run_service.py all call
    # bare .model_dump(mode="json")). A plain dump of an old-style capability WILL now carry a
    # `"resolved_schema": null` key it never carried before — pinned here explicitly so this is a
    # documented, deliberate consequence, not a silent surprise for #900's implementer.
    cap = OHMCapability(ref="core/web.search@1", binding="web.search")
    dumped = cap.model_dump(mode="json")
    assert "resolved_schema" in dumped
    assert dumped["resolved_schema"] is None


def test_resolved_schema_is_independent_of_config() -> None:
    # resolved_schema is a SIBLING of config, never merged into it (ADR-053 rejects OHMCapability
    # .config as the carrier — see "Rejected: OHMCapability.config").
    cap = OHMCapability(
        ref="core/web.search@1",
        binding="web.search",
        config={"credential_mappings": {"api_key": "secret-ref"}},
        resolved_schema=_SCHEMA,
    )
    assert cap.config == {"credential_mappings": {"api_key": "secret-ref"}}
    assert cap.resolved_schema == _SCHEMA
