# oraclous-governance

Organisation-context propagation utilities (Layer 1, ADR-006).

Resolves and propagates the request-scoped `OrganisationContext` (ORA-14, story 0f):

- `context` — the immutable `OrganisationContext`, the authenticated `Principal`/`PrincipalType`, the `MembershipResolver` seam, and `resolve_organisation_context(...)`. The `organisation_id` is sourced from an auth-issued claim or a fail-closed membership lookup, never from the request body.
- `propagation` — binds a resolved context for the request via a `contextvars.ContextVar` (`use_organisation_context` / `current_organisation_context`); reading an unbound context fails closed.
