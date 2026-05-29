"""Failing tests for credential-broker internal-service-key hardening (ORA-33, R1-B2).

Behavioural reference: legacy ``credential-broker-service/app/core/config.py``,
which ships a hardcoded internal service key
(``INTERNAL_SERVICE_KEY: str = "THEINTERNALSERVICEKEYISNONSECRETATTHEMOMENT"``).
The Reshape removes that insecure default so the key is sourced from secret
management (the environment / a secret manager), failing closed when it is
absent.

Pins the first acceptance criterion and Structured Threat Catalogue T6
(operator separation, ADR-008): platform staff must not hold a baked-in service
credential. The full customer-KMS-envelope reshape is explicitly out of scope
here (deferred to R8).

``test_placeholder_internal_service_key_absent_from_source`` is an active guard
(it scans the shipped source and runs today). The two behavioural tests import
inside the test body — they are RED until ``backend-implementer`` creates
``oraclous_credential_broker_service.core.config`` with a default-free,
environment-sourced ``INTERNAL_SERVICE_KEY``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.operator_separation]

# The legacy hardcoded value that must never reappear in the shipped source.
_LEGACY_PLACEHOLDER = "THEINTERNALSERVICEKEYISNONSECRETATTHEMOMENT"  # noqa: S105

_SERVICE_SRC = Path(__file__).resolve().parents[2] / "src"

# Required settings that are not under test here; supplied so that constructing
# ``Settings`` exercises only the INTERNAL_SERVICE_KEY contract.
_OTHER_REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
    "ENCRYPTION_KEY": "dGVzdC1lbmNyeXB0aW9uLWtleQ==",  # noqa: S105 — test value, not a real key
}


def test_placeholder_internal_service_key_absent_from_source() -> None:
    """The legacy hardcoded key must not survive the lift into the new source."""
    offenders = [
        str(path.relative_to(_SERVICE_SRC))
        for path in _SERVICE_SRC.rglob("*.py")
        if _LEGACY_PLACEHOLDER in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"hardcoded internal service key found in: {offenders}; "
        "source it from secret management instead"
    )


def test_internal_service_key_has_no_insecure_default() -> None:
    """``INTERNAL_SERVICE_KEY`` must be required (no baked-in default) — fail closed."""
    from oraclous_credential_broker_service.core.config import Settings

    field = Settings.model_fields["INTERNAL_SERVICE_KEY"]
    assert field.is_required(), (
        "INTERNAL_SERVICE_KEY must have no default so it is sourced from secret "
        "management; a default lets the service start with a known key"
    )


def test_internal_service_key_sourced_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When provided by the environment, the key is read from there verbatim."""
    for name, value in _OTHER_REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "injected-by-secret-manager")

    from oraclous_credential_broker_service.core.config import Settings

    assert Settings().INTERNAL_SERVICE_KEY == "injected-by-secret-manager"
