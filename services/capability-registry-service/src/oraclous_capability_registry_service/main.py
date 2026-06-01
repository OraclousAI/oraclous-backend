"""ASGI entrypoint for capability-registry-service.

Uvicorn target: ``oraclous_capability_registry_service.main:app``
"""

from oraclous_capability_registry_service.app.factory import create_app

app = create_app()
