"""
[tests] harness capability allocation — unit — ORAA-77

Story: ORAA-77 / ORA-76
Architecture refs:
  - R2 release page (T2-M3):    https://oraclous.atlassian.net/wiki/spaces/OP/pages/688482
  - OHM v1.0 Spec:              https://oraclous.atlassian.net/wiki/spaces/OP/pages/393501
  - Test Strategy:              https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940
  - ADR-010 (TDD):              https://oraclous.atlassian.net/wiki/spaces/OP/pages/557078

All imports from app.services.capability_allocation are done function-locally per
ADR-010 / CLAUDE.md §4.1 — pytest collection succeeds and each test fails at runtime
with ImportError (RED-by-design, on its own marker only).

Until the implementer creates app/services/capability_allocation.py, every test
here fails individually at runtime with ImportError or AttributeError.

Threat: T2-M3 — allocation cannot include a capability whose credential scope exceeds
the harness's declared scope.  Unit tests exercise the pure scope-comparison logic
(no DB required); the integration suite covers full allocate() end-to-end.

Behaviours covered:
  A01  HarnessCapabilityAllocator is importable from app.services.capability_allocation
  A02  ScopeViolationError is importable from app.services.capability_allocation
  A03  InvocationHandle is importable from app.services.capability_allocation
  A04  check_scope_compliance is importable from app.services.capability_allocation
  A05  InvocationHandle has capability_id, kind, and descriptor attributes
  A06  ScopeViolationError carries harness_id, capability_id, and violating_scopes
  A07  check_scope_compliance() with empty credential_requirements does not raise
  A08  check_scope_compliance() with api_key requirement does not raise
  A09  check_scope_compliance() with connection_string requirement does not raise
  A10  check_scope_compliance() with username_password requirement does not raise
  A11  check_scope_compliance() passes when oauth scopes are a strict subset
  A12  check_scope_compliance() passes when oauth scopes exactly match harness scope
  A13  check_scope_compliance() raises ScopeViolationError when scope not in harness scope
  A14  check_scope_compliance() raises with empty harness scope and oauth requirement
  A15  ScopeViolationError.violating_scopes identifies the specific rejected scope(s)
  A16  check_scope_compliance() raises on violation, not silently accumulates
  A17  multiple credential_requirements: all oauth entries are checked
"""

from __future__ import annotations

import uuid

import pytest

# ---------------------------------------------------------------------------
# A01  HarnessCapabilityAllocator is importable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_harness_capability_allocator_is_importable() -> None:
    """HarnessCapabilityAllocator must be importable from app.services.capability_allocation."""
    # function-local: ADR-010
    from app.services.capability_allocation import HarnessCapabilityAllocator

    assert HarnessCapabilityAllocator is not None


# ---------------------------------------------------------------------------
# A02  ScopeViolationError is importable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scope_violation_error_is_importable() -> None:
    """ScopeViolationError must be importable from app.services.capability_allocation."""
    # function-local: ADR-010
    from app.services.capability_allocation import ScopeViolationError

    assert ScopeViolationError is not None


# ---------------------------------------------------------------------------
# A03  InvocationHandle is importable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invocation_handle_is_importable() -> None:
    """InvocationHandle must be importable from app.services.capability_allocation."""
    # function-local: ADR-010
    from app.services.capability_allocation import InvocationHandle

    assert InvocationHandle is not None


# ---------------------------------------------------------------------------
# A04  check_scope_compliance is importable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_scope_compliance_is_importable() -> None:
    """check_scope_compliance must be importable from app.services.capability_allocation."""
    # function-local: ADR-010
    from app.services.capability_allocation import check_scope_compliance

    assert callable(check_scope_compliance)


# ---------------------------------------------------------------------------
# A05  InvocationHandle has capability_id, kind, descriptor attributes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invocation_handle_has_required_attributes() -> None:
    """InvocationHandle must expose capability_id, kind, and descriptor attributes.

    These are the minimum fields the harness runtime needs to resolve and invoke
    a capability at execution time (R4 — harness runtime extraction).
    """
    # function-local: ADR-010
    from app.models.capability_descriptor import DescriptorKind
    from app.services.capability_allocation import InvocationHandle

    cap_id = uuid.uuid4()
    descriptor = {
        "kind": "tool",
        "id": "test-tool",
        "version": {"hash": "sha256:abc123", "tags": []},
        "metadata": {"name": "Test Tool", "description": "A test."},
        "spec": {
            "implementation": {"type": "internal", "handler": "test.Tool"},
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "credential_requirements": [],
        },
    }
    handle = InvocationHandle(
        capability_id=cap_id,
        kind=DescriptorKind.TOOL,
        descriptor=descriptor,
    )
    assert handle.capability_id == cap_id
    assert handle.kind == DescriptorKind.TOOL
    assert handle.descriptor == descriptor


# ---------------------------------------------------------------------------
# A06  ScopeViolationError carries harness_id, capability_id, violating_scopes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scope_violation_error_carries_structured_context() -> None:
    """ScopeViolationError must carry harness_id, capability_id, and violating_scopes.

    These fields are required for the audit trail (T7) — a scope violation must be
    attributable to the harness and capability that triggered it, and must identify
    which scopes were rejected for operator diagnosis.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import ScopeViolationError

    harness_id = uuid.uuid4()
    capability_id = uuid.uuid4()
    err = ScopeViolationError(
        harness_id=harness_id,
        capability_id=capability_id,
        violating_scopes=["google.drive:write"],
    )
    assert err.harness_id == harness_id
    assert err.capability_id == capability_id
    assert "google.drive:write" in err.violating_scopes


# ---------------------------------------------------------------------------
# A07  check_scope_compliance() with empty credential_requirements does not raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_scope_compliance_empty_requirements_never_raises() -> None:
    """A capability with no credential_requirements never triggers a scope violation.

    A tool that requires no credentials is safe to allocate to any harness regardless
    of the harness's declared scope.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import check_scope_compliance

    check_scope_compliance(
        harness_id=uuid.uuid4(),
        capability_id=uuid.uuid4(),
        harness_declared_scope=[],
        credential_requirements=[],
    )


# ---------------------------------------------------------------------------
# A08  check_scope_compliance() with api_key requirement does not raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_scope_compliance_api_key_requirement_never_raises() -> None:
    """api_key credential requirements are not scope-checked (T2-M3 targets oauth_token only).

    API keys are credential-type access controls; scope enforcement applies only to
    oauth_token requirements which carry a declared scope list.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import check_scope_compliance

    check_scope_compliance(
        harness_id=uuid.uuid4(),
        capability_id=uuid.uuid4(),
        harness_declared_scope=[],
        credential_requirements=[
            {"type": "api_key", "provider": "openai", "scopes": None},
        ],
    )


# ---------------------------------------------------------------------------
# A09  check_scope_compliance() with connection_string requirement does not raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_scope_compliance_connection_string_requirement_never_raises() -> None:
    """connection_string credential requirements are not scope-checked (T2-M3: oauth_token only)."""
    # function-local: ADR-010
    from app.services.capability_allocation import check_scope_compliance

    check_scope_compliance(
        harness_id=uuid.uuid4(),
        capability_id=uuid.uuid4(),
        harness_declared_scope=[],
        credential_requirements=[
            {"type": "connection_string", "provider": "postgres", "scopes": None},
        ],
    )


# ---------------------------------------------------------------------------
# A10  check_scope_compliance() with username_password requirement does not raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_scope_compliance_username_password_requirement_never_raises() -> None:
    """username_password credential requirements are not scope-checked (T2-M3: oauth only)."""
    # function-local: ADR-010
    from app.services.capability_allocation import check_scope_compliance

    check_scope_compliance(
        harness_id=uuid.uuid4(),
        capability_id=uuid.uuid4(),
        harness_declared_scope=[],
        credential_requirements=[
            {"type": "username_password", "provider": "mysql", "scopes": None},
        ],
    )


# ---------------------------------------------------------------------------
# A11  check_scope_compliance() passes when oauth scope is a strict subset
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_scope_compliance_passes_when_scope_is_subset() -> None:
    """Scope check passes when the capability's oauth scopes are a strict subset of harness scope.

    Harness declares ["drive:read", "drive:write"]; capability requires only ["drive:read"]
    — the allocation is within bounds.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import check_scope_compliance

    check_scope_compliance(
        harness_id=uuid.uuid4(),
        capability_id=uuid.uuid4(),
        harness_declared_scope=["drive:read", "drive:write"],
        credential_requirements=[
            {"type": "oauth_token", "provider": "google", "scopes": ["drive:read"]},
        ],
    )


# ---------------------------------------------------------------------------
# A12  check_scope_compliance() passes when oauth scopes exactly match harness scope
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_scope_compliance_passes_on_exact_match() -> None:
    """Scope check passes when the capability's oauth scopes exactly match the harness scope."""
    # function-local: ADR-010
    from app.services.capability_allocation import check_scope_compliance

    check_scope_compliance(
        harness_id=uuid.uuid4(),
        capability_id=uuid.uuid4(),
        harness_declared_scope=["drive:read"],
        credential_requirements=[
            {"type": "oauth_token", "provider": "google", "scopes": ["drive:read"]},
        ],
    )


# ---------------------------------------------------------------------------
# A13  check_scope_compliance() raises ScopeViolationError when scope not in harness scope
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_scope_compliance_raises_when_scope_not_declared() -> None:
    """T2-M3: raises ScopeViolationError when capability requires a scope not in harness scope.

    A harness that declares only "drive:read" cannot allocate a tool that requires
    "drive:write" — doing so would silently grant write access not authorised.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import ScopeViolationError, check_scope_compliance

    with pytest.raises(ScopeViolationError):
        check_scope_compliance(
            harness_id=uuid.uuid4(),
            capability_id=uuid.uuid4(),
            harness_declared_scope=["drive:read"],
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:write"]},
            ],
        )


# ---------------------------------------------------------------------------
# A14  check_scope_compliance() raises when harness scope is empty and oauth required
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_scope_compliance_raises_when_harness_has_no_scope() -> None:
    """T2-M3: zero-scope harness cannot allocate any oauth-credential capability.

    An empty declared scope means the harness is not authorised to use any
    OAuth-backed tool.  Attempting to allocate one is rejected.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import ScopeViolationError, check_scope_compliance

    with pytest.raises(ScopeViolationError):
        check_scope_compliance(
            harness_id=uuid.uuid4(),
            capability_id=uuid.uuid4(),
            harness_declared_scope=[],
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:read"]},
            ],
        )


# ---------------------------------------------------------------------------
# A15  ScopeViolationError.violating_scopes identifies the specific rejected scope(s)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_scope_violation_error_identifies_specific_violating_scopes() -> None:
    """ScopeViolationError.violating_scopes must list only the scopes that were rejected.

    This is the T2-M3 audit signal: operators need to know exactly which scopes were
    denied, not just that a violation occurred.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import ScopeViolationError, check_scope_compliance

    with pytest.raises(ScopeViolationError) as exc_info:
        check_scope_compliance(
            harness_id=uuid.uuid4(),
            capability_id=uuid.uuid4(),
            harness_declared_scope=["drive:read"],
            credential_requirements=[
                {
                    "type": "oauth_token",
                    "provider": "google",
                    "scopes": ["drive:read", "drive:write"],
                },
            ],
        )
    err = exc_info.value
    assert "drive:write" in err.violating_scopes, (
        "ScopeViolationError.violating_scopes must contain 'drive:write'; "
        f"got: {err.violating_scopes!r}"
    )
    # The allowed scope must NOT appear in violating_scopes
    assert "drive:read" not in err.violating_scopes, (
        "Permitted scope 'drive:read' must not appear in violating_scopes"
    )


# ---------------------------------------------------------------------------
# A16  check_scope_compliance() raises on violation, does not silently accumulate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_scope_compliance_raises_not_silently_accumulates() -> None:
    """check_scope_compliance() raises on any scope violation — does not return a list.

    A harness is either fully within its scope or it is not.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import ScopeViolationError, check_scope_compliance

    with pytest.raises(ScopeViolationError):
        check_scope_compliance(
            harness_id=uuid.uuid4(),
            capability_id=uuid.uuid4(),
            harness_declared_scope=["drive:read"],
            credential_requirements=[
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:write"]},
                {"type": "oauth_token", "provider": "github", "scopes": ["repo:write"]},
            ],
        )


# ---------------------------------------------------------------------------
# A17  check_scope_compliance() with multiple requirements: all oauth are checked
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_scope_compliance_checks_all_oauth_requirements() -> None:
    """When a capability has multiple credential_requirements, every oauth_token entry is checked.

    A capability with one in-scope and one out-of-scope oauth requirement must still
    raise ScopeViolationError.
    """
    # function-local: ADR-010
    from app.services.capability_allocation import ScopeViolationError, check_scope_compliance

    with pytest.raises(ScopeViolationError):
        check_scope_compliance(
            harness_id=uuid.uuid4(),
            capability_id=uuid.uuid4(),
            # Harness allows drive:read and github:read, but not github:write
            harness_declared_scope=["drive:read", "github:read"],
            credential_requirements=[
                # First requirement is OK
                {"type": "oauth_token", "provider": "google", "scopes": ["drive:read"]},
                # Second requirement violates — github:write not declared
                {"type": "oauth_token", "provider": "github", "scopes": ["github:write"]},
            ],
        )
