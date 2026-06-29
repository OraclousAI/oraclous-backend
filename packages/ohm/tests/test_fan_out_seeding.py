"""#599 — a ``fan_out.over: "$.<key>"`` resolves to a real list, two complementary ways.

Today ``_resolve_over`` returns ``[]`` for both shapes, so a fan-out NEVER expands. This pins the
two legs the engine threads:

  LEG 1 (user input) — a user-seeded ``run_team(state={"items": [...]})`` value (a BARE list) drives
  a member's ``fan_out.over: "$.items"``: the member dispatches ONCE PER ITEM.

  LEG 2 (producer output) — an upstream producer member ``A`` returns a list ``output`` (its
  dispatch result is WRAPPED ``{"output": [...], "status": ...}``); a downstream ``B`` with
  ``fan_out.over: "$.A"`` UNWRAPS that ``output`` and dispatches once per produced item.

The assertion is the dispatched item COUNT == the list length — proof the fan-out actually expanded.
RED until the [impl] makes ``_resolve_over`` read state-or-unwrapped-producer-output.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from oraclous_ohm.envelope import HandoffEnvelope
from oraclous_ohm.manifest import OHMFanOut, OHMManifest, OHMMember, OHMMetadata, OHMRuntime
from oraclous_ohm.orchestrate import _resolve_over, run_team

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


def _m(
    role: str, *, depends_on: list[str] | None = None, fan_out: OHMFanOut | None = None
) -> OHMMember:
    return OHMMember(
        role=role,
        kind="agent",
        manifest_ref=f"org:x/{role}@1",
        depends_on=depends_on or [],
        fan_out=fan_out,
    )


def _team(members: list[OHMMember]) -> OHMManifest:
    return OHMManifest(
        ohm_version="1.1",
        metadata=OHMMetadata(id=uuid.uuid4(), name="t", owner_organization_id=_ORG, kind="team"),
        members=members,
        runtime=OHMRuntime(entrypoint=members[0].role),
    )


# ── _resolve_over: the unit boundary, both shapes ────────────────────────────────────────────


def test_resolve_over_reads_a_user_seeded_state_list() -> None:
    # LEG 1: a bare list in state resolves directly (state wins over results when the key exists)
    assert _resolve_over("$.items", {"items": ["a", "b", "c"]}, {}) == ["a", "b", "c"]


def test_resolve_over_parses_a_producer_json_array_string() -> None:
    # LEG 2 (the REAL path): a producer member's harness `output` is TEXT — an LLM outliner emits a
    # JSON array as a string; _resolve_over parses the embedded array (no fake list anywhere).
    results = {"A": {"output": '["x", "y", "z"]', "status": "SUCCEEDED"}}
    assert _resolve_over("$.A", {}, results) == ["x", "y", "z"]


def test_resolve_over_parses_a_json_array_embedded_in_prose() -> None:
    # real models wrap the array in prose / a ```json fence — the embedded array is still extracted
    results = {"A": {"output": 'Here are the chapters:\n```json\n["a", "b"]\n```'}}
    assert _resolve_over("$.A", {}, results) == ["a", "b"]


def test_resolve_over_unwraps_an_already_list_producer_output() -> None:
    # a structured/tool member whose `output` is already a genuine list is used as-is (back-compat)
    results = {"A": {"output": ["x", "y"], "status": "SUCCEEDED"}}
    assert _resolve_over("$.A", {}, results) == ["x", "y"]


def test_resolve_over_returns_empty_for_a_non_list() -> None:
    # fail-soft: a missing key, or a non-list value, yields no fan-out items (not an error)
    assert _resolve_over("$.missing", {}, {}) == []
    assert _resolve_over("$.A", {}, {"A": {"output": "not-a-list"}}) == []


def test_resolve_over_falls_back_to_a_bare_producer_value() -> None:
    # a producer that returned a bare (unwrapped) list — no `output` key — is used as-is
    assert _resolve_over("$.A", {}, {"A": ["one", "two"]}) == ["one", "two"]


# ── run_team: the end-to-end fan-out expansion, both legs ────────────────────────────────────


async def test_user_seeded_state_drives_a_fan_out_once_per_item() -> None:
    # LEG 1 e2e: state={"items": [...]} + a member fan_out.over="$.items" → dispatched once per item
    seen: list[Any] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        seen.append(item)
        return {"output": item, "status": "SUCCEEDED"}

    members = [_m("w", fan_out=OHMFanOut(over="$.items", max_parallel=4, reduce="concat"))]
    res = await run_team(_team(members), dispatch, state={"items": ["i1", "i2", "i3"]})
    assert res.status == "completed"
    assert seen == ["i1", "i2", "i3"]  # ONE dispatch per seeded item — the fan-out expanded
    assert len(seen) == 3  # count == list length


async def test_producer_output_list_drives_a_downstream_fan_out() -> None:
    # LEG 2 e2e (the REAL path): producer A's harness output is a JSON-array STRING (what an LLM
    # emits — not a Python list); B fan_out.over="$.A" parses it + fans over the parsed items.
    fan_items: list[Any] = []

    async def dispatch(member: OHMMember, envs: list[HandoffEnvelope], item: Any) -> dict:
        if member.role == "A":  # the producer emits its list as JSON text (a real LLM output)
            return {"output": '["p1", "p2", "p3", "p4"]', "status": "SUCCEEDED"}
        fan_items.append(item)  # B's per-item dispatches
        return {"output": item, "status": "SUCCEEDED"}

    members = [
        _m("A"),  # producer — returns a list
        _m("B", depends_on=["A"], fan_out=OHMFanOut(over="$.A", max_parallel=4, reduce="concat")),
    ]
    res = await run_team(_team(members), dispatch)
    assert res.status == "completed"
    assert fan_items == ["p1", "p2", "p3", "p4"]  # B dispatched once per PRODUCED item (unwrapped)
    assert len(fan_items) == 4  # count == produced list length
