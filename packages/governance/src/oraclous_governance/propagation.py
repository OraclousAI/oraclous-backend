"""Request-scoped organisation-context propagation (Layer 1, ADR-006).

A resolved ``OrganisationContext`` is bound for the duration of a request via a
``contextvars.ContextVar`` so downstream substrate calls read it without it being
threaded through every call signature. Reading the context when none is bound
fails closed (raises) rather than defaulting — a missing organisation scope must
halt, never silently widen access (Structured Threat Catalogue T1-M1).

Because the binding is a ``ContextVar`` it propagates across ``await`` to awaited
callees, but does not leak across ``asyncio`` task boundaries: each task runs in a
copy of the context, so concurrent requests never observe each other's
organisation.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar

from oraclous_governance.context import OrganisationContext

_ORGANISATION_CONTEXT: ContextVar[OrganisationContext] = ContextVar("oraclous_organisation_context")


class MissingOrganisationContextError(RuntimeError):
    """Raised when the organisation context is read but none is bound."""


def current_organisation_context() -> OrganisationContext:
    """Return the organisation context bound to the current scope.

    Fails closed: raises ``MissingOrganisationContextError`` when none is bound,
    never returns ``None`` or a default.
    """
    try:
        return _ORGANISATION_CONTEXT.get()
    except LookupError as exc:
        raise MissingOrganisationContextError(
            "no organisation context is bound in the current scope"
        ) from exc


@contextlib.contextmanager
def use_organisation_context(
    context: OrganisationContext,
) -> Iterator[OrganisationContext]:
    """Bind ``context`` for the duration of the ``with`` block.

    Nesting is supported: the previous binding (if any) is restored on exit, so an
    inner scope never clobbers an outer one.
    """
    token = _ORGANISATION_CONTEXT.set(context)
    try:
        yield context
    finally:
        _ORGANISATION_CONTEXT.reset(token)
