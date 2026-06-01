# app/services/instance_manager.py
from typing import Any, Dict, List, Optional
from uuid import UUID
import logging

from app.interfaces.instance_manager import BaseInstanceManager
from app.repositories.instance_repository import InstanceRepository
from app.services.credential_client import CredentialClient
from app.schemas.tool_instance import (
    ToolInstance,
    CreateInstanceRequest,
    UpdateInstanceRequest,
    ConfigureCredentialsRequest,
    InstanceCredentialStatus,
    InstanceStatusResponse,
    Execution,
    CreateExecutionRequest,
)
from app.schemas.common import InstanceStatus
from app.schemas.tool_definition import ToolDefinition

logger = logging.getLogger(__name__)


class InstanceManagerService(BaseInstanceManager):
    """
    Service for managing tool instances and their lifecycle
    """

    def __init__(
        self,
        instance_repo: InstanceRepository,
        tool_registry: Any,
        credential_client: CredentialClient,
    ):
        self.repo = instance_repo
        self.tool_registry = tool_registry
        self.credential_client = credential_client

    async def create_instance(
        self,
        user_id: UUID,
        tool_definition_id: str,
        workflow_id: str,
        configuration: Dict[str, Any],
    ) -> ToolInstance:
        """Create a new tool instance with automatic credential check"""
        # 1. Validate tool definition exists
        tool_definition = await self.tool_registry.get_tool(tool_definition_id)
        if not tool_definition:
            raise ValueError(f"Tool definition {tool_definition_id} not found")

        # 2. Validate configuration against tool schema
        if not self._validate_configuration(tool_definition, configuration):
            raise ValueError("Invalid configuration for tool")

        # 3. Create instance request
        create_request = CreateInstanceRequest(
            workflow_id=workflow_id,
            tool_definition_id=tool_definition_id,
            name=configuration.get("name", tool_definition.name),
            description=configuration.get("description"),
            configuration=configuration,
            settings=configuration.get("settings", {}),
        )

        # 4. Create instance in repository
        instance = await self.repo.create_instance(user_id, create_request)

        # 5. Determine and store required credentials
        required_creds = self._extract_required_credentials(tool_definition)

        if required_creds:
            await self._update_required_credentials(instance.id, required_creds)

        # 6. Try to fetch credentials from broker
        credential_mappings = {}
        oauth_redirects = {}
        missing_credentials = []
        required_scopes = self._build_required_scopes_map(tool_definition)
        for cred_req in tool_definition.credential_requirements:
            cred_type = cred_req.type.value
            provider = "google"
            scopes = required_scopes.get(cred_type, []) if required_scopes else []
            if cred_type == "OAUTH_TOKEN" and provider:
                # Try to get runtime token for the actual provider
                token_response = await self.credential_client.get_runtime_token(
                    user_id=str(user_id), provider=str(provider), required_scopes=scopes
                )
                print("Token Response:", token_response)
                if token_response and token_response.get("access_token"):
                    credential_mappings[cred_type] = provider
                else:
                    # Fallback: validate and get login URL
                    validation = await self.credential_client.validate_credentials(
                        user_id=str(user_id),
                        credential_mappings={cred_type: provider},
                        required_scopes=required_scopes,
                    )
                    login_url = token_response.get("login_url")
                    print("Login URL:", login_url)
                    oauth_redirects[cred_type] = login_url
                    missing_credentials.append(cred_type)
            elif cred_type == "OAUTH_TOKEN":
                # If provider is missing, treat as missing credential
                missing_credentials.append(cred_type)
            else:
                validation = await self.credential_client.validate_credentials(
                    user_id=user_id, credential_mappings={cred_type: cred_type}
                )
                valid = validation.get(cred_type, {}).get("valid", False)
                if valid:
                    credential_mappings[cred_type] = cred_type
                else:
                    missing_credentials.append(cred_type)

        # 7. Update instance status
        if not missing_credentials:
            await self.repo.update_instance_status(instance.id, InstanceStatus.READY)
        else:
            await self.repo.update_instance_status(
                instance.id, InstanceStatus.CONFIGURATION_REQUIRED
            )

        logger.info(f"Created instance {instance.id} for tool {tool_definition.name}")
        # Attach credential info to instance (for API response)
        instance = await self.repo.get_instance(instance.id)
        instance.oauth_redirects = oauth_redirects
        instance.missing_credentials = missing_credentials
        return instance

    async def configure_instance(
        self, instance_id: str, user_id: UUID, configuration: Dict[str, Any]
    ) -> bool:
        """Update instance configuration"""
        # 1. Get existing instance
        instance = await self.repo.get_user_instance(instance_id, user_id)
        if not instance:
            raise ValueError(f"Instance {instance_id} not found for user {user_id}")

        # 2. Get tool definition for validation
        tool_definition = await self.tool_registry.get_tool(instance.tool_definition_id)
        if not tool_definition:
            raise ValueError(f"Tool definition {instance.tool_definition_id} not found")

        # 3. Validate configuration
        if not self._validate_configuration(tool_definition, configuration):
            raise ValueError("Invalid configuration for tool")

        # 4. Update instance
        update_request = UpdateInstanceRequest(configuration=configuration)
        updated_instance = await self.repo.update_instance(
            instance_id, user_id, update_request
        )

        if updated_instance:
            logger.info(f"Updated configuration for instance {instance_id}")
            return True

        return False

    async def configure_credentials(
        self, instance_id: str, user_id: UUID, request: ConfigureCredentialsRequest
    ) -> InstanceStatusResponse:
        """Configure credentials for an instance, with OAuth/manual handling"""
        # 1. Get instance
        instance = await self.repo.get_user_instance(instance_id, user_id)
        if not instance:
            raise ValueError(f"Instance {instance_id} not found for user {user_id}")

        # 2. Get tool definition
        tool_definition = await self.tool_registry.get_tool(instance.tool_definition_id)
        if not tool_definition:
            raise ValueError(f"Tool definition {instance.tool_definition_id} not found")

        # 3. Validate credential mappings
        validation_errors = []
        required_cred_types = {
            req.type.value
            for req in tool_definition.credential_requirements
            if req.required
        }
        provided_cred_types = set(request.credential_mappings.keys())

        # Check for missing required credentials
        missing_creds = required_cred_types - provided_cred_types
        if missing_creds:
            validation_errors.extend(
                [f"Missing required credential: {cred}" for cred in missing_creds]
            )

        # Check for unexpected credentials
        unexpected_creds = provided_cred_types - {
            req.type.value for req in tool_definition.credential_requirements
        }
        if unexpected_creds:
            validation_errors.extend(
                [f"Unexpected credential: {cred}" for cred in unexpected_creds]
            )

        if validation_errors:
            raise ValueError(
                f"Credential validation failed: {'; '.join(validation_errors)}"
            )

        # 4. Validate credentials with credential broker
        required_scopes = self._build_required_scopes_map(tool_definition)
        credential_validation = await self.credential_client.validate_credentials(
            user_id=user_id,
            credential_mappings=request.credential_mappings,
            required_scopes=required_scopes,
        )

        # 5. Update instance with credential mappings
        updated_instance = await self.repo.configure_credentials(
            instance_id, user_id, request.credential_mappings
        )

        if not updated_instance:
            raise ValueError("Failed to update instance credentials")

        # 6. Build credential status response, handle OAuth/manual
        credentials_status = []
        all_valid = True
        oauth_redirects = {}
        missing_credentials = []

        for cred_req in tool_definition.credential_requirements:
            cred_type = cred_req.type.value
            mapping_exists = cred_type in request.credential_mappings
            validation_result = credential_validation.get(cred_type, {})
            is_valid = (
                validation_result.get("valid", False) if mapping_exists else False
            )

            if cred_type == "OAUTH_TOKEN" and not is_valid:
                login_url = validation_result.get("login_url")
                if login_url:
                    oauth_redirects[cred_type] = login_url
                missing_credentials.append(cred_type)
            elif cred_req.required and not is_valid:
                missing_credentials.append(cred_type)

            if cred_req.required and not is_valid:
                all_valid = False

            credentials_status.append(
                InstanceCredentialStatus(
                    credential_type=cred_type,
                    required=cred_req.required,
                    configured=mapping_exists,
                    valid=is_valid,
                    error_message=validation_result.get("error")
                    if mapping_exists
                    else None,
                )
            )

        # 7. Update instance status
        if all_valid:
            await self.repo.update_instance_status(instance_id, InstanceStatus.READY)
            updated_instance.status = InstanceStatus.READY
        else:
            await self.repo.update_instance_status(
                instance_id, InstanceStatus.CONFIGURATION_REQUIRED
            )
            updated_instance.status = InstanceStatus.CONFIGURATION_REQUIRED

        logger.info(
            f"Configured credentials for instance {instance_id}, status: {updated_instance.status}"
        )

        # Attach credential info to instance (for API response)
        updated_instance.oauth_redirects = oauth_redirects
        updated_instance.missing_credentials = missing_credentials

        return InstanceStatusResponse(
            instance=updated_instance,
            credentials_status=credentials_status,
            is_ready_for_execution=all_valid,
            validation_errors=[],
        )

    async def get_instance(self, instance_id: str) -> Optional[ToolInstance]:
        """Retrieve tool instance by ID"""
        return await self.repo.get_instance(instance_id)

    async def get_user_instance(
        self, instance_id: str, user_id: UUID
    ) -> Optional[ToolInstance]:
        """Retrieve user's tool instance by ID"""
        return await self.repo.get_user_instance(instance_id, user_id)

    async def list_user_instances(
        self,
        user_id: UUID,
        workflow_id: Optional[str] = None,
        status: Optional[InstanceStatus] = None,
        page: int = 0,
        size: int = 50,
    ) -> tuple[List[ToolInstance], int]:
        """List user's instances with filtering"""
        return await self.repo.list_instances(
            user_id=user_id,
            workflow_id=workflow_id,
            status=status,
            page=page,
            size=size,
        )

    async def validate_instance_ready(self, instance_id: str) -> bool:
        """Check if instance is ready for execution"""
        instance = await self.repo.get_instance(instance_id)
        if not instance:
            return False

        return instance.status == InstanceStatus.READY

    async def get_instance_status(
        self, instance_id: str, user_id: UUID
    ) -> InstanceStatusResponse:
        """Get complete status information for an instance"""
        # 1. Get instance
        instance = await self.repo.get_user_instance(instance_id, user_id)
        if not instance:
            raise ValueError(f"Instance {instance_id} not found for user {user_id}")

        # 2. Get tool definition
        tool_definition = await self.tool_registry.get_tool(instance.tool_definition_id)
        if not tool_definition:
            raise ValueError(f"Tool definition {instance.tool_definition_id} not found")

        # 3. Validate current credentials if configured
        credentials_status = []
        validation_errors = []
        all_ready = True

        if instance.credential_mappings:
            required_scopes = self._build_required_scopes_map(tool_definition)
            credential_validation = await self.credential_client.validate_credentials(
                user_id=user_id,
                credential_mappings=instance.credential_mappings,
                required_scopes=required_scopes,
            )

            for cred_req in tool_definition.credential_requirements:
                cred_type = cred_req.type.value
                mapping_exists = cred_type in instance.credential_mappings
                validation_result = credential_validation.get(cred_type, {})
                is_valid = (
                    validation_result.get("valid", False) if mapping_exists else False
                )

                if cred_req.required and not is_valid:
                    all_ready = False
                    if not mapping_exists:
                        validation_errors.append(
                            f"Missing required credential: {cred_type}"
                        )
                    else:
                        validation_errors.append(
                            f"Invalid credential {cred_type}: {validation_result.get('error', 'Unknown error')}"
                        )

                credentials_status.append(
                    InstanceCredentialStatus(
                        credential_type=cred_type,
                        required=cred_req.required,
                        configured=mapping_exists,
                        valid=is_valid,
                        error_message=validation_result.get("error")
                        if mapping_exists
                        else None,
                    )
                )
        else:
            # No credentials configured yet
            for cred_req in tool_definition.credential_requirements:
                cred_type = cred_req.type.value
                if cred_req.required:
                    all_ready = False
                    validation_errors.append(
                        f"Missing required credential: {cred_type}"
                    )

                credentials_status.append(
                    InstanceCredentialStatus(
                        credential_type=cred_type,
                        required=cred_req.required,
                        configured=False,
                        valid=False,
                        error_message="Not configured",
                    )
                )

        return InstanceStatusResponse(
            instance=instance,
            credentials_status=credentials_status,
            is_ready_for_execution=all_ready
            and instance.status == InstanceStatus.READY,
            validation_errors=validation_errors,
        )

    async def delete_instance(self, instance_id: str, user_id: UUID) -> bool:
        """Delete an instance"""
        result = await self.repo.delete_instance(instance_id, user_id)
        if result:
            logger.info(f"Deleted instance {instance_id} for user {user_id}")
        return result

    async def create_execution(
        self,
        instance_id: str,
        user_id: UUID,
        input_data: Dict[str, Any],
        max_retries: int = 3,
    ) -> Execution:
        """Create an execution for an instance"""
        # 1. Validate instance is ready
        if not await self.validate_instance_ready(instance_id):
            raise ValueError(f"Instance {instance_id} is not ready for execution")

        # 2. Create execution request
        request = CreateExecutionRequest(
            instance_id=instance_id, input_data=input_data, max_retries=max_retries
        )

        # 3. Create execution record
        execution = await self.repo.create_execution(user_id, request)

        logger.info(f"Created execution {execution.id} for instance {instance_id}")
        return execution

    # ================== HELPER METHODS ==================

    def _validate_configuration(
        self, tool_definition: ToolDefinition, configuration: Dict[str, Any]
    ) -> bool:
        """Validate configuration against tool schema"""
        # Basic validation - in production, implement proper JSON schema validation
        if not tool_definition.configuration_schema:
            return True  # No schema to validate against

        # TODO: Implement proper JSON schema validation
        # For now, just check that configuration is a dict
        return isinstance(configuration, dict)

    def _extract_required_credentials(
        self, tool_definition: ToolDefinition
    ) -> List[str]:
        """Extract required credential types from tool definition"""
        return [
            req.type.value
            for req in tool_definition.credential_requirements
            if req.required
        ]

    async def _update_required_credentials(
        self, instance_id: str, required_credentials: List[str]
    ) -> None:
        """Update the required credentials list for an instance"""
        from datetime import datetime

        updates = {
            "required_credentials": required_credentials,
            "updated_at": datetime.utcnow(),
        }

        # This is a direct DB update since we don't have a specific method for this
        from sqlalchemy import update
        from app.models.tool_instance import ToolInstanceDB

        query = (
            update(ToolInstanceDB)
            .where(ToolInstanceDB.id == instance_id)
            .values(**updates)
        )

        await self.repo.db.execute(query)
        await self.repo.db.commit()

    def _build_required_scopes_map(
        self, tool_definition: ToolDefinition
    ) -> Dict[str, List[str]]:
        """Build mapping of credential_type -> required_scopes"""
        scopes_map = {}

        for cred_req in tool_definition.credential_requirements:
            if cred_req.scopes:
                scopes_map[cred_req.type.value] = cred_req.scopes

        return scopes_map
