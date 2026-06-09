"""ORM models (ORAA-4 §21). Importing the package registers every table on ``Base.metadata``
(consumed by Alembic's ``env.py``)."""

from __future__ import annotations

from oraclous_application_gateway_service.models.base_model import Base
from oraclous_application_gateway_service.models.chat import ChatMessage, ChatThread
from oraclous_application_gateway_service.models.integration_key import IntegrationKey
from oraclous_application_gateway_service.models.published_agent import PublishedAgent

__all__ = ["Base", "ChatMessage", "ChatThread", "IntegrationKey", "PublishedAgent"]
