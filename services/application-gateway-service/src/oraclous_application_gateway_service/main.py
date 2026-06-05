"""ASGI entrypoint for application-gateway-service.

Uvicorn target: ``oraclous_application_gateway_service.main:app``
"""

from oraclous_application_gateway_service.app.factory import create_app
from oraclous_application_gateway_service.core.lifespan import lifespan

app = create_app(lifespan=lifespan)
