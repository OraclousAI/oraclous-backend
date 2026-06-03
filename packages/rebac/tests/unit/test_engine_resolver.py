"""Failing tests for the ReBAC engine resolver adapter (ORA-46).

The adapter satisfies the ORA-15 substrate seam's ``RelationResolver`` protocol —
``async def resolve(AccessRequest) -> bool | None`` — by dispatching to the
ORA-34 ``ReBACEngine``'s ``check_graph_permission``. The seam remains the
single fail-closed chokepoint; the adapter:

* maps the seam's vocabulary onto the engine's: ``organisation_id→organisation_id``,
  ``subject→subject`` (as ``{"type": "user", "id": <value>}``),
  ``resource→graph_id``, ``relation→required_level`` via a
  **defined** lookup into ``{read, write, admin}`` (not identity);
* returns ``None`` for unknown relations and for out-of-domain subjects
  (non-``user-``) and resources (non-``graph-``) — never a best-effort call into
  the engine;
* lets underlying exceptions propagate without pre-collapsing them to ``False``,
  so the seam's exception handler produces a deny with a reason distinguishable
  from a definitive absent (the load-bearing pin from the AC).

RED until ``backend-implementer`` creates ``oraclous_rebac.ReBACEngineResolver``.

The ``permission_check`` is injectable as a callable so these unit tests do not
need a real ``ReBACEngine`` instance — the contract is the dispatch shape, not
the engine internals (those are covered in ``test_rebac_engine.py``).

NB module-level imports of ``oraclous_rebac`` adapter symbols are *function-local*
to satisfy ``tools/lint/check_test_imports.py`` (TST001): the adapter does not
yet exist, and a module-level import would abort collection for the whole run.
"""

from __future__ import annotations

import pytest
from oraclous_substrate.rebac import (
    AccessDecision,
    AccessDecisionClient,
    AccessRequest,
)

pytestmark = [pytest.mark.unit, pytest.mark.rebac]


_ORG = "org-aaaa"
_USER = "user-1234"
_GRAPH = "graph-9999"


def _request(
    relation: str = "read",
    *,
    organisation_id: str = _ORG,
    subject: str = _USER,
    resource: str = _GRAPH,
) -> AccessRequest:
    return AccessRequest(
        organisation_id=organisation_id,
        subject=subject,
        resource=resource,
        relation=relation,
    )


class _RecordingCheck:
    """A test double for the engine's ``check_graph_permission`` callable.

    Records each call's kwargs (so argument mapping is assertable) and returns
    a configured ``bool``, or raises a configured exception. The signature
    mirrors ``ReBACEngine.check_graph_permission`` — driver positional, then
    keyword-only ``organisation_id``, ``subject``, ``graph_id``,
    ``required_level`` — so a wiring mistake (e.g. passing the relation
    verbatim, or threading the wrong field as ``subject``) surfaces here.
    """

    def __init__(
        self,
        *,
        result: bool = False,
        raises: Exception | None = None,
    ) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        driver: object,
        *,
        organisation_id: str,
        subject: dict,
        graph_id: str,
        required_level: str,
    ) -> bool:
        self.calls.append(
            {
                "driver": driver,
                "organisation_id": organisation_id,
                "subject": subject,
                "graph_id": graph_id,
                "required_level": required_level,
            }
        )
        if self._raises is not None:
            raise self._raises
        return self._result


def _make_adapter(check: _RecordingCheck, *, driver: object | None = None):
    """Construct the not-yet-built adapter. Import is function-local — see the
    module docstring on the TDD-window collection guardrail.
    """
    from oraclous_rebac import ReBACEngineResolver  # noqa: PLC0415

    return ReBACEngineResolver(permission_check=check, driver=driver)


# ── Resolver returns True / False / None at the adapter level ───────────────


async def test_returns_true_when_engine_authorises() -> None:
    """A True from the underlying permission check flows through as True."""
    check = _RecordingCheck(result=True)
    resolver = _make_adapter(check)
    assert await resolver.resolve(_request()) is True


async def test_returns_false_when_engine_definitively_denies() -> None:
    """A definitive False (relation absent) flows through as False — NOT None.

    The adapter must preserve the engine's bool semantics so the seam can
    distinguish "definitive deny" from "ambiguous" if it ever needs to. The
    AC mandates: ``bool → bool``; ``error → None or raise``.
    """
    check = _RecordingCheck(result=False)
    resolver = _make_adapter(check)
    assert await resolver.resolve(_request()) is False


# ── Argument mapping (the load-bearing pin) ─────────────────────────────────


async def test_argument_mapping_pins_field_correspondence() -> None:
    """ORA-46 AC: organisation_id→organisation_id, subject→subject (dict),
    resource→graph_id, relation→required_level (via defined lookup).

    A wiring mistake here would silently authorise the wrong tenant or wrong
    resource and is impossible to detect downstream — pin it explicitly.
    """
    check = _RecordingCheck(result=True)
    sentinel_driver = object()
    resolver = _make_adapter(check, driver=sentinel_driver)

    await resolver.resolve(
        AccessRequest(
            organisation_id="org-xxxx",
            subject="user-alice",
            resource="graph-roadmap",
            relation="write",
        )
    )

    assert len(check.calls) == 1, "engine called exactly once on a recognised request"
    call = check.calls[0]
    assert call["organisation_id"] == "org-xxxx"
    assert call["subject"] == {"type": "user", "id": "user-alice"}
    assert call["graph_id"] == "graph-roadmap", "resource must map to graph_id"
    assert call["required_level"] == "write", "relation must map via the defined lookup"
    assert call["driver"] is sentinel_driver, "configured driver is threaded through"


@pytest.mark.parametrize("relation", ["read", "write", "admin"])
async def test_relation_lookup_covers_the_defined_levels(relation: str) -> None:
    """The relation→required_level lookup is exactly {read, write, admin} —
    identity on these names — and is *defined* (the AC's emphasis: not implicit
    pass-through). Adding a relation beyond these requires editing the adapter,
    which is the point: an unknown relation must fail closed (next test).
    """
    check = _RecordingCheck(result=True)
    resolver = _make_adapter(check)
    await resolver.resolve(_request(relation=relation))
    assert check.calls[0]["required_level"] == relation


# ── Fail-closed: unknown relation, out-of-domain subject, out-of-domain resource ─


async def test_unknown_relation_returns_none_and_skips_engine() -> None:
    """An unmapped relation must not call the engine — it returns None so the
    seam denies. A best-effort engine call would leak that "we tried" and
    might match a relation we did not intend to recognise.
    """
    check = _RecordingCheck(result=True)  # would allow if called — proves we didn't
    resolver = _make_adapter(check)
    assert await resolver.resolve(_request(relation="totally-unknown-relation")) is None
    assert check.calls == [], "engine must not be consulted for unknown relations"


@pytest.mark.parametrize(
    "non_user_subject",
    [
        "agent-bot-7",  # ORA-27 C2 future, but not in this story's scope
        "service-cron-1",
        "1234",  # no prefix at all
        "USER-1234",  # case matters; convention is lowercase
    ],
)
async def test_non_user_subject_returns_none_and_skips_engine(
    non_user_subject: str,
) -> None:
    """Out-of-domain subjects (non-``user-``) fail closed without an engine
    call. The brief is explicit: "never a best-effort engine call". Agent
    subjects land in ORA-27 C2 with their own resolver, not this one.
    """
    check = _RecordingCheck(result=True)
    resolver = _make_adapter(check)
    assert await resolver.resolve(_request(subject=non_user_subject)) is None
    assert check.calls == []


@pytest.mark.parametrize(
    "non_graph_resource",
    [
        "harness-runtime-7",
        "capability-summariser",
        "9999",  # no prefix at all
        "GRAPH-9999",  # case matters
    ],
)
async def test_non_graph_resource_returns_none_and_skips_engine(
    non_graph_resource: str,
) -> None:
    """Out-of-domain resources (non-``graph-``) fail closed without an engine
    call. Non-graph resource types are out of scope for this story (the brief).
    """
    check = _RecordingCheck(result=True)
    resolver = _make_adapter(check)
    assert await resolver.resolve(_request(resource=non_graph_resource)) is None
    assert check.calls == []


# ── Return translation: engine errors must NOT pre-collapse to False ────────


async def test_engine_error_propagates_not_collapsed_to_false() -> None:
    """The load-bearing AC: engine error must surface as an exception (or
    None), not be pre-collapsed into ``False``. The seam (``AccessDecisionClient``)
    is the single chokepoint that converts errors to deny — pre-collapsing
    here would erase the distinction between "definitively absent" and
    "indeterminate / errored", which is how legacy ``check_graph_permission``
    accidentally turned every Neo4j blip into an unattributable deny.
    """
    err = RuntimeError("Neo4j store down")
    check = _RecordingCheck(raises=err)
    resolver = _make_adapter(check)
    with pytest.raises(RuntimeError, match="Neo4j store down"):
        await resolver.resolve(_request())


# ── End-to-end through AccessDecisionClient: distinguishable reasons ────────


async def test_seam_end_to_end_allow_definitive_deny_error_deny_reasons_distinguishable() -> None:
    """Through ``AccessDecisionClient`` an allow, a definitive deny, and an
    error-deny must produce three *observably distinct* decisions — the
    error-deny must carry a different ``reason`` from the definitive deny
    (the AC's "deny carrying a reason, distinct from a definitive deny").

    This is the integration of the seam's three branches with the adapter:

      adapter returns True  → AccessDecision.allow(reason="…present…")
      adapter returns False → AccessDecision.deny(reason="…absent or ambiguous…")
      adapter raises        → AccessDecision.deny(reason="…resolver error…")

    Without this distinction, a "Neo4j is down" deny and a "user has no role"
    deny look identical to callers, masking outages as access failures and
    burying T1 instrumentation signals.
    """
    allow_resolver = _make_adapter(_RecordingCheck(result=True))
    deny_resolver = _make_adapter(_RecordingCheck(result=False))
    error_resolver = _make_adapter(_RecordingCheck(raises=RuntimeError("store down")))

    allow_decision: AccessDecision = await AccessDecisionClient(resolver=allow_resolver).check(
        _request()
    )
    deny_decision: AccessDecision = await AccessDecisionClient(resolver=deny_resolver).check(
        _request()
    )
    error_decision: AccessDecision = await AccessDecisionClient(resolver=error_resolver).check(
        _request()
    )

    # Verdicts in the correct direction.
    assert allow_decision.allowed is True
    assert deny_decision.allowed is False
    assert error_decision.allowed is False

    # Reasons pairwise distinguishable — the load-bearing assertion.
    reasons = {
        "allow": allow_decision.reason,
        "deny": deny_decision.reason,
        "error": error_decision.reason,
    }
    assert len({reasons["allow"], reasons["deny"], reasons["error"]}) == 3, (
        f"expected three distinguishable reasons, got {reasons!r}"
    )
    # The deny reason must not look like an error reason — a Neo4j outage
    # masquerading as "relation absent" is the bug this test exists to prevent.
    assert "absent" in deny_decision.reason or "ambiguous" in deny_decision.reason
    assert "error" in error_decision.reason


async def test_seam_accepts_adapter_as_relation_resolver() -> None:
    """The adapter must be structurally usable as the seam's ``RelationResolver``
    — i.e. ``AccessDecisionClient(resolver=adapter)`` accepts it and a
    ``.check()`` call dispatches through it. This is the "is injectable into
    AccessDecisionClient(resolver=…)" AC at runtime; import-linter cleanliness
    is asserted by the CI gate, not by a test.
    """
    check = _RecordingCheck(result=True)
    adapter = _make_adapter(check)
    client = AccessDecisionClient(resolver=adapter)
    decision = await client.check(_request())
    assert decision.allowed is True
    assert len(check.calls) == 1, "the seam actually dispatched into the adapter"


# ── Tenancy: the seam owns blank-org rejection; the adapter does not bypass ─


async def test_blank_organisation_id_is_rejected_at_seam_not_swallowed_by_adapter() -> None:
    """ADR-006: the seam owns the blank-``organisation_id`` rejection
    (``test_check_rejects_blank_organisation_id`` in the substrate suite). The
    adapter must not silently allow a blank org_id by short-circuiting before
    the seam validates — using the adapter through the seam must still raise.
    """
    resolver = _make_adapter(_RecordingCheck(result=True))
    client = AccessDecisionClient(resolver=resolver)
    with pytest.raises(ValueError):
        await client.check(_request(organisation_id=""))
