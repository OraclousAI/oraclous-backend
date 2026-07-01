"""Cadence-aware cost pre-flight projection (domain layer; #603, ADR-048 dec-4(a)).

A PURE, I/O-free forward projection — the dual of the harness-runtime BACKWARD spend estimate
(``SpendService.estimate``): for each priced team member, ``price(binding, expected_in/out tokens)``
multiplied by ``fires_per_day(cron)``, summed → a per-member breakdown + a fleet "~$X/day" total.
It prices the WORST case (every member fires every window — the full pool), never the happy
path. Unknown/unset bindings are reported UNPRICED (never $0 — the price is never fabricated;
``oraclous_ohm.billing.price`` fails closed). Deterministic: the same (members, cron, tokens) always
yields the same number, so the "~$X/day" surfaced BEFORE GO is reproducible.

The price table is the shared, canonical ADR-044 seam (``oraclous_ohm.billing``) — the engine can
price without importing harness-runtime (both are independent Layer-3 services). ``croniter`` lives
here (engine-side), NOT in the shared kernel.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from croniter import croniter
from oraclous_ohm.billing import SCHEDULED_SCAN_DEFAULT_BINDING, price
from oraclous_ohm.manifest import OHMManifest

# A FIXED Monday anchor + a 28-day (= exactly 4 weeks) window so the cadence count is deterministic
# and cadence-intrinsic: hourly → 24/day, daily → 1/day, weekly → 4/28 = ~0.143/day (a float, NEVER
# rounded to 0), */5-min → 288/day. Monthly is approximated (~1/28). Determinism matters: the CTO
# must be able to reproduce the exact "~$X/day".
_ANCHOR = datetime(2025, 1, 6, tzinfo=UTC)  # a Monday
_WINDOW_DAYS = 28

#: default per-member, per-fire token expectations when the caller supplies none — the projection is
#: a "~$X/day" ESTIMATE, so a rough per-agent turn is fine; the endpoint exposes these as overrides.
DEFAULT_EXPECTED_INPUT_TOKENS = 4000
DEFAULT_EXPECTED_OUTPUT_TOKENS = 1000


@dataclass(frozen=True, slots=True)
class MemberCost:
    """One team member's projected recurring cost. ``priced`` is False (and both usd fields None)
    when the member's binding is unset/unknown — reported unpriced, never a fabricated $0."""

    role: str
    binding: str | None
    priced: bool
    usd_per_fire: float | None
    usd_per_day: float | None


@dataclass(frozen=True, slots=True)
class ScheduleCostProjection:
    """The forward "~$X/day at this cadence" projection. ``fleet_usd_per_day`` sums ONLY the priced
    members; ``unpriced_members`` lists the roles whose binding could not be priced (so a 0/partial
    total is never mistaken for "free")."""

    cadence_fires_per_day: float
    fleet_usd_per_day: float
    per_member: list[MemberCost]
    unpriced_members: list[str]


def fires_per_day(cron: str, *, now: datetime | None = None) -> float:
    """How many times a cron fires per day (a float — a weekly cadence is ~0.143/day, never 0).

    Counts fire instants over a fixed 28-day window and normalises to per-day. ``now`` (an aware
    anchor) overrides the fixed anchor for testing; production leaves it None for a deterministic,
    reproducible number. A malformed cron raises ``ValueError`` (the route maps it to a 422)."""
    if not cron or not croniter.is_valid(cron):
        raise ValueError(f"invalid cron expression: {cron!r}")
    anchor = now or _ANCHOR
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    end = anchor + timedelta(days=_WINDOW_DAYS)
    itr = croniter(cron, anchor)
    count = 0
    while itr.get_next(datetime) <= end:
        count += 1
    return count / _WINDOW_DAYS


def _sub_primary_binding(sub: Mapping[str, Any]) -> str | None:
    """The primary model binding DECLARED in an inline sub-harness, read straight off the dict —
    mirrors ``OHMManifest.primary_model`` (the ``role=="primary"`` model, else the first). Read
    directly (NOT via ``load_ohm``) so a sub that DECLARES an expensive model but is otherwise
    malformed is still priced at that model, never silently dropped to the cheaper default and
    understating the pre-flight (CTO/review #603)."""
    models = sub.get("models")
    if not isinstance(models, list) or not models:
        return None
    primary = next((m for m in models if isinstance(m, dict) and m.get("role") == "primary"), None)
    if primary is None and isinstance(models[0], dict):
        primary = models[0]
    binding = primary.get("binding") if isinstance(primary, dict) else None
    return binding if isinstance(binding, str) and binding else None


def resolve_member_binding(
    role: str,
    team: OHMManifest,
    sub_harnesses: Mapping[str, Any],
    default_binding: str | None,
) -> str | None:
    """The model binding a member will RUN with, in precedence: (1) its inline sub-harness DECLARED
    primary model (what actually dispatches), (2) the team manifest's ``model_by_role``, (3) the
    cheaper scheduled-scan default (dec-4(c)) — or ``None`` when the default is disabled, in which
    case an unset member is unpriced. Only a member with NO declared model anywhere takes the
    default; a declared (even in a malformed sub) binding always wins, so the estimate never
    understates a member that set an expensive model."""
    sub = sub_harnesses.get(role)
    if isinstance(sub, Mapping):
        declared = _sub_primary_binding(sub)
        if declared is not None:
            return declared
    team_model = team.model_by_role(role)
    if team_model is not None:
        return team_model.binding
    return default_binding


def apply_scheduled_scan_default(
    manifest: Mapping[str, Any],
    sub_harnesses: Mapping[str, Any],
    *,
    default_binding: str = SCHEDULED_SCAN_DEFAULT_BINDING,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """#603 dec-4(c): at TEAM-schedule FIRE time, give each AGENT member whose model is UNSET the
    cheaper scan default. **Declared-binding-always-wins** — a member whose inline sub-harness
    already declares a model is left byte-for-byte untouched. A human gate (no LLM), and a
    ``manifest_ref`` member with no inline sub-harness (it runs a registered harness with its own
    model, out of reach here), are also left as-is.

    Pure: returns NEW dicts (a shallow copy of ``sub_harnesses`` with a fresh sub for each member it
    stamps); the caller's dicts are never mutated, and the STORED schedule manifest is never
    rewritten — this is a per-fire default, not a stored-manifest mutation. ``manifest`` is passed
    through unchanged (a regular member's model rides its sub-harness, not the team ``models``)."""
    revised_subs: dict[str, Any] = dict(sub_harnesses)
    for member in manifest.get("members") or []:
        if not isinstance(member, dict) or member.get("kind") == "human":
            continue
        role = member.get("role")
        if not isinstance(role, str):
            continue
        sub = revised_subs.get(role)
        if not isinstance(sub, dict):
            continue  # no inline sub-harness → manifest_ref member, out of 4(c)'s reach
        models = sub.get("models") or []
        if any(isinstance(m, dict) and m.get("binding") for m in models):
            continue  # a declared model wins — untouched
        revised_subs[role] = {
            **sub,
            "models": [
                {
                    "role": "primary",
                    "binding": default_binding,
                    "protocol_shape": "openai-compatible",
                }
            ],
        }
    return dict(manifest), revised_subs


def project_schedule_cost(
    team: OHMManifest,
    sub_harnesses: Mapping[str, Any],
    cron: str,
    *,
    expected_in: int = DEFAULT_EXPECTED_INPUT_TOKENS,
    expected_out: int = DEFAULT_EXPECTED_OUTPUT_TOKENS,
    default_binding: str | None = SCHEDULED_SCAN_DEFAULT_BINDING,
    now: datetime | None = None,
) -> ScheduleCostProjection:
    """Project the team's recurring "~$X/day at this cadence". Prices only AGENT members (a human
    gate incurs no LLM cost and is skipped, never priced at the scan default). Worst case: every
    agent member fires every window. Unpriced members contribute 0 to the fleet total AND are listed
    in ``unpriced_members`` (a 0 is never a fabricated price)."""
    fpd = fires_per_day(cron, now=now)
    per_member: list[MemberCost] = []
    unpriced: list[str] = []
    fleet = 0.0
    for member in team.members:
        if member.kind == "human":  # a human gate is not an LLM cost — skip (never scan-defaulted)
            continue
        binding = resolve_member_binding(member.role, team, sub_harnesses, default_binding)
        result = price(binding, expected_in, expected_out)
        if result.priced and result.usd is not None:
            usd_per_fire = result.usd
            usd_per_day = usd_per_fire * fpd
            fleet += usd_per_day
            per_member.append(MemberCost(member.role, binding, True, usd_per_fire, usd_per_day))
        else:
            unpriced.append(member.role)
            per_member.append(MemberCost(member.role, binding, False, None, None))
    return ScheduleCostProjection(
        cadence_fires_per_day=fpd,
        fleet_usd_per_day=fleet,
        per_member=per_member,
        unpriced_members=unpriced,
    )
