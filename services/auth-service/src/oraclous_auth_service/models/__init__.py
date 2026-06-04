"""ORM model registry. Importing this package registers every table on ``Base.metadata`` so Alembic
autogenerate + ``upgrade head`` and ``create_all`` all see the full schema.
"""

from oraclous_auth_service.models.agent_model import Agent, AgentCredential
from oraclous_auth_service.models.base import Base
from oraclous_auth_service.models.organisation_model import Organisation, OrgMember
from oraclous_auth_service.models.refresh_token_model import RefreshToken
from oraclous_auth_service.models.user_model import User

__all__ = [
    "Agent",
    "AgentCredential",
    "Base",
    "Organisation",
    "OrgMember",
    "RefreshToken",
    "User",
]
