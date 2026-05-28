"""Failing tests for organisation-context propagation (ORA-14, story 0f).

Scope: the propagation utilities of ``packages/governance`` — how a resolved
``OrganisationContext`` is bound for the duration of a request and read by
downstream substrate calls. The object + resolution are pinned in
``test_organisation_context.py``.

Pins AC#2 (obtaining context when none is set fails closed — raises, never
defaults) and AC#3 (propagation works across async boundaries: request →
substrate call). The concurrent-task isolation tests pin the per-organisation
boundary (ADR-006, Structured Threat Catalogue T1-M1): one request's
organisation context must never leak into another's.

RED until backend-implementer creates ``oraclous_governance.propagation``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from oraclous_governance.context import OrganisationContext, PrincipalType
from oraclous_governance.propagation import (
    MissingOrganisationContextError,
    current_organisation_context,
    use_organisation_context,
)

pytestmark = [pytest.mark.unit]


def _ctx(organisation_id: uuid.UUID | None = None) -> OrganisationContext:
    return OrganisationContext(
        organisation_id=organisation_id or uuid.uuid4(),
        principal_id=uuid.uuid4(),
        principal_type=PrincipalType.USER,
    )


def test_current_context_without_one_set_fails_closed() -> None:
    """AC#2: reading the context when none is bound raises — never returns None or
    a default. A missing organisation context must halt, not silently widen
    access (fail-closed)."""
    with pytest.raises(MissingOrganisationContextError):
        current_organisation_context()


def test_context_is_readable_within_scope() -> None:
    ctx = _ctx()
    with use_organisation_context(ctx):
        assert current_organisation_context() is ctx


def test_context_is_cleared_after_scope() -> None:
    """The binding is request-scoped: it does not leak past the ``with`` block."""
    ctx = _ctx()
    with use_organisation_context(ctx):
        assert current_organisation_context() is ctx
    with pytest.raises(MissingOrganisationContextError):
        current_organisation_context()


def test_nested_scopes_restore_the_outer_context() -> None:
    outer, inner = _ctx(), _ctx()
    with use_organisation_context(outer):
        assert current_organisation_context() is outer
        with use_organisation_context(inner):
            assert current_organisation_context() is inner
        assert current_organisation_context() is outer


async def test_context_propagates_to_awaited_callee() -> None:
    """AC#3: a context bound in the request scope is visible to a downstream
    awaited (substrate) call without being threaded through the call arguments."""
    ctx = _ctx()

    async def substrate_call() -> uuid.UUID:
        await asyncio.sleep(0)
        return current_organisation_context().organisation_id

    with use_organisation_context(ctx):
        result = await substrate_call()
    assert result == ctx.organisation_id


@pytest.mark.organization_isolation
async def test_concurrent_requests_do_not_leak_context() -> None:
    """T1-M1: two requests running concurrently each see only their own
    organisation context — no cross-request leakage even when interleaved."""
    ctx_a = _ctx()
    ctx_b = _ctx()
    seen: dict[str, uuid.UUID] = {}

    async def handle(name: str, ctx: OrganisationContext) -> None:
        with use_organisation_context(ctx):
            await asyncio.sleep(0)  # force interleave with the sibling task
            seen[name] = current_organisation_context().organisation_id

    await asyncio.gather(handle("a", ctx_a), handle("b", ctx_b))
    assert seen["a"] == ctx_a.organisation_id
    assert seen["b"] == ctx_b.organisation_id
    assert seen["a"] != seen["b"]


@pytest.mark.organization_isolation
async def test_context_does_not_leak_from_child_task_to_parent() -> None:
    """Binding a context inside a child task must not bind it in the parent: the
    parent remains without a context (fail-closed) after the child completes."""

    async def child() -> None:
        with use_organisation_context(_ctx()):
            await asyncio.sleep(0)

    await asyncio.create_task(child())
    with pytest.raises(MissingOrganisationContextError):
        current_organisation_context()
