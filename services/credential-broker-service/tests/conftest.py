"""Credential-broker local test conftest.

Provides the ``postgres_dsn`` fixture (a session-scoped ephemeral Postgres testcontainer) for this
service's integration suite, mirroring the root harness so the suite runs in isolation.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

POSTGRES_IMAGE = "postgres:16"
PG_USER = "oraclous"
PG_PASSWORD = "oraclous"  # noqa: S105 — ephemeral test container, not a real secret
PG_DB = "oraclous"


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """A libpq DSN for an ephemeral Postgres container."""
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        POSTGRES_IMAGE, username=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )
    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        yield f"postgresql://{PG_USER}:{PG_PASSWORD}@{host}:{port}/{PG_DB}"


def make_test_envelope():  # noqa: ANN201
    """A fully-working envelope for tests (ADR-020): a LocalKmsProvider with a fresh KEK + an
    in-memory DEK store (no DB needed). encrypt → v2, decrypt → polymorphic (v2 via the DEK, v1 via
    the legacy single key). Used wherever a service constructor now needs ``envelope=``."""
    import base64
    import os
    from types import SimpleNamespace

    from oraclous_credential_broker_service.core.envelope import LocalKmsProvider
    from oraclous_credential_broker_service.core.security import decrypt_secret
    from oraclous_credential_broker_service.services.envelope_service import EnvelopeService

    class _MemDekRepo:
        def __init__(self) -> None:
            self._rows: dict = {}

        async def get_for_org(self, *, organisation_id):  # noqa: ANN001, ANN202
            return self._rows.get(organisation_id)

        async def create(self, *, organisation_id, wrapped_dek, kek_provider, kek_key_id):  # noqa: ANN001, ANN202
            row = SimpleNamespace(
                organisation_id=organisation_id,
                wrapped_dek=wrapped_dek,
                kek_provider=kek_provider,
                kek_key_id=kek_key_id,
            )
            self._rows[organisation_id] = row
            return row

    kek = base64.b64encode(os.urandom(32)).decode("ascii")
    return EnvelopeService(
        kms=LocalKmsProvider(kek), dek_repo=_MemDekRepo(), legacy_decrypt=decrypt_secret
    )


@pytest.fixture
def test_envelope():  # noqa: ANN201
    """A fresh working envelope per test (ADR-020); inject where a service now needs envelope."""
    return make_test_envelope()
