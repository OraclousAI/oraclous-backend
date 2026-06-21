"""uvicorn entrypoint — production composition of the credential-broker app."""

from __future__ import annotations

from oraclous_credential_broker_service.app.factory import create_app
from oraclous_credential_broker_service.core.lifespan import lifespan

app = create_app(lifespan=lifespan)
