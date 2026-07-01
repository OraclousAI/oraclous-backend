"""#603 (E8) — cadence-aware cost pre-flight: "~$X/day at this cadence" BEFORE GO (via the gateway).

ADR-048 dec-4(a): a standing fleet shows its forward recurring cost before it is enabled (#389 Item
14 / O2 P0). Dec-4(c): a member whose model is UNSET is priced at the cheaper scan default.

DETERMINISTIC — the projection is a pure multiply-add over the shipped ADR-044 price table, so the
"~$X/day" is an EXACT number the CTO can reproduce (no LLM, no BYOM needed). Real user, through the
application-gateway ``:8006`` only (never a service port / ``/internal``); nothing mocked. The pre-
flight creates NOTHING (asserted: the schedule list is unchanged across the call).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_MINI = "openrouter/openai/gpt-4o-mini"  # 0.15 / 0.60 per Mtok (in the ADR-044 table)
_UNKNOWN = "openrouter/acme/not-in-the-rate-table"  # absent → unpriced (never $0)
_SCAN_DEFAULT = "openrouter/google/gemini-1.5-flash"  # the dec-4(c) cheaper scan default


def _agent(role: str) -> dict:
    return {
        "role": role,
        "kind": "agent",
        "manifest_ref": f"x/{role}@1",
        "subgoal": "s",
        "depends_on": [],
        "tools": [],
    }


def _sub(org: str, binding: str) -> dict:
    return {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": "s", "owner_organization_id": org},
        "prompts": [{"role": "primary", "source": "inline", "body": "go"}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "models": [{"role": "primary", "binding": binding, "protocol_shape": "openai-compatible"}],
        "runtime": {"entrypoint": "primary"},
    }


def _bare_sub(org: str) -> dict:
    # a valid single-agent sub-harness with NO model — the dec-4(c) scan-default target.
    return {
        "ohm_version": "1.0",
        "metadata": {"id": str(uuid.uuid4()), "name": "s", "owner_organization_id": org},
        "prompts": [{"role": "primary", "source": "inline", "body": "go"}],
        "actors": [{"role": "primary", "kind": "agent"}],
        "runtime": {"entrypoint": "primary"},
    }


def _team(org: str, roles: list[str]) -> dict:
    return {
        "ohm_version": "1.1",
        "metadata": {
            "id": str(uuid.uuid4()),
            "name": "fleet",
            "owner_organization_id": org,
            "kind": "team",
        },
        "members": [_agent(r) for r in roles],
        "runtime": {"entrypoint": roles[0]},
    }


def test_cost_preflight_projects_dollars_per_day_before_go(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"pre{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    org = user["org_id"]

    schedules_before = c.get("/v1/engine/schedules").json()

    # a 3-member fleet: one priced (gpt-4o-mini), one unpriceable, one UNSET (→ scan default).
    body = {
        "manifest": _team(org, ["priced", "unknown", "unset"]),
        "cron": "0 * * * *",  # hourly → 24 fires/day (deterministic)
        "input_data": {
            "sub_harnesses": {
                "priced": _sub(org, _MINI),
                "unknown": _sub(org, _UNKNOWN),
                # 'unset' has NO sub-harness model → the dec-4(c) scan default is applied
                "unset": _bare_sub(org),
            }
        },
        "expected_input_tokens": 1_000_000,
        "expected_output_tokens": 1_000_000,
    }
    r = c.post("/v1/engine/schedules/preflight", json=body)
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["currency"] == "USD"
    assert data["cadence_fires_per_day"] == pytest.approx(24.0)  # hourly cadence
    per = {m["role"]: m for m in data["per_member"]}

    # priced: gpt-4o-mini @ 1M+1M = 0.75/fire × 24 = 18.00/day (EXACT — reproducible)
    assert per["priced"]["priced"] is True
    assert per["priced"]["usd_per_day"] == pytest.approx(18.00)

    # dec-4(c): the UNSET member is priced at the cheaper scan default (gemini-flash), NOT unpriced
    assert per["unset"]["priced"] is True
    assert per["unset"]["binding"] == _SCAN_DEFAULT
    assert per["unset"]["usd_per_day"] == pytest.approx(
        (0.075 + 0.30) * 24
    )  # 0.375/fire × 24 = 9.00

    # the unknown binding is UNPRICED — reported, never a fabricated $0
    assert per["unknown"]["priced"] is False
    assert per["unknown"]["usd_per_day"] is None
    assert data["unpriced_members"] == ["unknown"]

    # the fleet "~$X/day" sums ONLY the priced members (18.00 + 9.00), never the unpriced one
    assert data["fleet_usd_per_day"] == pytest.approx(18.00 + 9.00)

    # READ-ONLY: the pre-flight created / enabled NO schedule
    schedules_after = c.get("/v1/engine/schedules").json()
    assert schedules_after == schedules_before


def test_cost_preflight_rejects_a_bad_cron_and_a_non_team(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"pre{uuid.uuid4().hex[:10]} u")
    c = gateway_client(user["token"])
    org = user["org_id"]

    bad_cron = c.post(
        "/v1/engine/schedules/preflight",
        json={"manifest": _team(org, ["a"]), "cron": "definitely not a cron"},
    )
    assert bad_cron.status_code == 422, bad_cron.text

    non_team = c.post(
        "/v1/engine/schedules/preflight",
        json={"manifest": _sub(org, _MINI), "cron": "0 9 * * *"},  # a single-agent OHM, not a team
    )
    assert non_team.status_code == 422, non_team.text
