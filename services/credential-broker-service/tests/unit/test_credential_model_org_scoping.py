"""Failing test for ``organisation_id`` on the credential storage model (R1-B2).

Behavioural reference: legacy
``credential-broker-service/app/models/credential_model.py`` (``UserCredential``),
which scopes a credential by ``user_id`` only. The Reshape adds ``organisation_id``
as the outermost tenancy scope, mirroring ADR-006 ("organisation_id on every
substrate primitive") and the ORG002 storage-model guardrail.

RED until ``backend-implementer`` adds ``organisation_id`` to
``oraclous_credential_broker_service.models.credential_model.UserCredential``.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.organization_isolation]


def test_user_credential_has_non_nullable_organisation_id_column() -> None:
    from oraclous_credential_broker_service.models.credential_model import UserCredential

    columns = UserCredential.__table__.columns
    assert "organisation_id" in columns, (
        "UserCredential must carry organisation_id as the outermost tenancy scope (ADR-006)"
    )

    org_column = columns["organisation_id"]
    assert org_column.nullable is False, "organisation_id must be NOT NULL"
    assert "UUID" in str(org_column.type).upper(), "organisation_id must be a UUID column"
