import logging
from typing import Any, Dict, List
from uuid import UUID

from app.services.credential_client import CredentialClient
from app.tools.registry import tool_registry
from app.schemas.tool_instance import ToolInstance

logger = logging.getLogger(__name__)


class ValidationService:
    """
    Enhanced validation service for tool execution readiness
    Handles credential validation, implementation availability, and user-friendly error messages
    """

    def __init__(
        self,
        credential_client: CredentialClient,
        tool_registry_service: Any,
    ):
        self.credential_client = credential_client
        self.tool_registry_service = tool_registry_service

    async def validate_execution_readiness(
        self, instance: ToolInstance, user_id: UUID
    ) -> Dict[str, Any]:
        """
        Comprehensive validation of tool instance readiness for execution
        Returns detailed validation report with actionable error messages
        """
        validation_report = {
            "is_ready": True,
            "instance_id": instance.id,
            "validations": {
                "tool_definition": {"status": "pending"},
                "implementation": {"status": "pending"},
                "credentials": {"status": "pending"},
                "configuration": {"status": "pending"},
            },
            "errors": [],
            "warnings": [],
            "action_items": [],
        }

        try:
            # 1. Validate tool definition exists
            await self._validate_tool_definition(instance, validation_report)

            # 2. Validate implementation availability
            await self._validate_implementation_availability(
                instance, validation_report
            )

            # 3. Validate credentials
            await self._validate_credentials_comprehensive(
                instance, user_id, validation_report
            )

            # 4. Validate configuration
            await self._validate_configuration(instance, validation_report)

            # 5. Set overall readiness
            validation_report["is_ready"] = len(validation_report["errors"]) == 0

            return validation_report

        except Exception as e:
            logger.error(f"Validation failed for instance {instance.id}: {str(e)}")
            validation_report["is_ready"] = False
            validation_report["errors"].append(
                {
                    "type": "VALIDATION_SERVICE_ERROR",
                    "message": f"Validation service error: {str(e)}",
                    "severity": "critical",
                }
            )
            return validation_report

    async def _validate_tool_definition(
        self, instance: ToolInstance, report: Dict[str, Any]
    ):
        """Validate tool definition exists and is accessible"""
        try:
            tool_definition = await self.tool_registry_service.get_tool(
                instance.tool_definition_id
            )

            if not tool_definition:
                report["validations"]["tool_definition"]["status"] = "failed"
                report["errors"].append(
                    {
                        "type": "TOOL_DEFINITION_NOT_FOUND",
                        "message": f"Tool definition '{instance.tool_definition_id}' not found in registry",
                        "severity": "critical",
                        "field": "tool_definition_id",
                    }
                )
                report["action_items"].append(
                    {
                        "action": "contact_support",
                        "message": "Contact support to restore missing tool definition",
                        "priority": "high",
                    }
                )
            else:
                report["validations"]["tool_definition"]["status"] = "passed"
                report["validations"]["tool_definition"]["tool_name"] = (
                    tool_definition.name
                )
                report["validations"]["tool_definition"]["tool_version"] = (
                    tool_definition.version
                )

        except Exception as e:
            report["validations"]["tool_definition"]["status"] = "error"
            report["errors"].append(
                {
                    "type": "TOOL_DEFINITION_ERROR",
                    "message": f"Error accessing tool definition: {str(e)}",
                    "severity": "critical",
                }
            )

    async def _validate_implementation_availability(
        self, instance: ToolInstance, report: Dict[str, Any]
    ):
        """Validate tool implementation is available in memory registry"""
        try:
            # Check in-memory registry
            executor_class = tool_registry.get_executor_class(
                instance.tool_definition_id
            )
            definition = tool_registry.get_definition(instance.tool_definition_id)

            if not executor_class:
                report["validations"]["implementation"]["status"] = "failed"
                report["errors"].append(
                    {
                        "type": "IMPLEMENTATION_NOT_AVAILABLE",
                        "message": f"Tool implementation not available for '{instance.tool_definition_id}'",
                        "severity": "critical",
                        "details": "The tool is defined but cannot be executed because the implementation is missing",
                    }
                )
                report["action_items"].append(
                    {
                        "action": "tool_unavailable",
                        "message": "This tool is temporarily unavailable. Please try again later or contact support.",
                        "priority": "high",
                    }
                )
            else:
                report["validations"]["implementation"]["status"] = "passed"
                report["validations"]["implementation"]["executor_class"] = (
                    executor_class.__name__
                )

                # Check if definition is synced
                if not definition:
                    report["warnings"].append(
                        {
                            "type": "DEFINITION_NOT_SYNCED",
                            "message": "Tool implementation exists but definition not synced to memory registry",
                            "severity": "medium",
                        }
                    )

        except Exception as e:
            report["validations"]["implementation"]["status"] = "error"
            report["errors"].append(
                {
                    "type": "IMPLEMENTATION_CHECK_ERROR",
                    "message": f"Error checking implementation availability: {str(e)}",
                    "severity": "critical",
                }
            )

    async def _validate_credentials_comprehensive(
        self, instance: ToolInstance, user_id: UUID, report: Dict[str, Any]
    ):
        """Comprehensive credential validation with user-friendly messages"""
        try:
            # Get tool definition for credential requirements
            tool_definition = await self.tool_registry_service.get_tool(
                instance.tool_definition_id
            )
            if not tool_definition:
                report["validations"]["credentials"]["status"] = "skipped"
                return

            credential_details = []
            has_critical_issues = False

            # Check each required credential
            for cred_req in tool_definition.credential_requirements:
                cred_detail = await self._validate_single_credential(
                    instance, user_id, cred_req, tool_definition.name
                )
                credential_details.append(cred_detail)

                if cred_req.required and not cred_detail["is_valid"]:
                    has_critical_issues = True

            report["validations"]["credentials"]["status"] = (
                "failed" if has_critical_issues else "passed"
            )
            report["validations"]["credentials"]["details"] = credential_details

            # Add errors and action items based on credential issues
            for cred_detail in credential_details:
                if not cred_detail["is_valid"] and cred_detail["required"]:
                    report["errors"].append(
                        {
                            "type": "CREDENTIAL_INVALID",
                            "message": cred_detail["user_message"],
                            "severity": "critical",
                            "field": f"credential_{cred_detail['type']}",
                        }
                    )

                    if cred_detail.get("action_url"):
                        report["action_items"].append(
                            {
                                "action": "fix_credential",
                                "message": cred_detail["action_message"],
                                "url": cred_detail["action_url"],
                                "credential_type": cred_detail["type"],
                                "priority": "high",
                            }
                        )

        except Exception as e:
            report["validations"]["credentials"]["status"] = "error"
            report["errors"].append(
                {
                    "type": "CREDENTIAL_VALIDATION_ERROR",
                    "message": f"Error validating credentials: {str(e)}",
                    "severity": "critical",
                }
            )

    async def _validate_single_credential(
        self, instance: ToolInstance, user_id: UUID, cred_req, tool_name: str
    ) -> Dict[str, Any]:
        """Validate a single credential requirement with detailed user messaging"""
        cred_type = cred_req.type.value
        cred_detail = {
            "type": cred_type,
            "required": cred_req.required,
            "is_configured": False,
            "is_valid": False,
            "user_message": "",
            "action_message": "",
            "action_url": None,
        }

        try:
            # Check if credential is configured in instance
            if cred_type not in instance.credential_mappings:
                cred_detail["user_message"] = (
                    self._get_credential_not_configured_message(cred_type, tool_name)
                )
                cred_detail["action_message"] = (
                    "Please configure this credential to use the tool"
                )
                return cred_detail

            cred_detail["is_configured"] = True
            cred_identifier = instance.credential_mappings[cred_type]

            # Validate the credential
            if cred_type == "OAUTH_TOKEN":
                validation_result = await self._validate_oauth_credential(
                    user_id, cred_identifier, cred_req.scopes or []
                )
            else:
                validation_result = await self._validate_non_oauth_credential(
                    cred_identifier
                )

            cred_detail["is_valid"] = validation_result["valid"]

            if not validation_result["valid"]:
                cred_detail["user_message"] = self._get_credential_invalid_message(
                    cred_type, tool_name, validation_result.get("error")
                )
                cred_detail["action_message"] = self._get_credential_fix_message(
                    cred_type
                )
                cred_detail["action_url"] = validation_result.get("login_url")
            else:
                cred_detail["user_message"] = (
                    f"{self._get_credential_display_name(cred_type)} is properly configured"
                )

        except Exception as e:
            cred_detail["user_message"] = (
                f"Error checking {self._get_credential_display_name(cred_type)}: {str(e)}"
            )
            logger.error(f"Error validating credential {cred_type}: {str(e)}")

        return cred_detail

    async def _validate_oauth_credential(
        self, user_id: UUID, provider: str, scopes: List[str]
    ) -> Dict[str, Any]:
        """Validate OAuth credential with scope checking"""
        try:
            # Try to get runtime token
            token_data = await self.credential_client.get_runtime_token(
                user_id=user_id, provider=provider, required_scopes=scopes
            )

            if token_data and token_data.get("success"):
                return {"valid": True}
            token_data["valid"] = False
            return token_data
        except Exception as e:
            return {
                "valid": False,
                "error": str(e),
                "error_type": "OAUTH_VALIDATION_ERROR",
            }

    async def _validate_non_oauth_credential(
        self, credential_id: str
    ) -> Dict[str, Any]:
        """Validate non-OAuth credential"""
        try:
            return await self.credential_client._validate_credential(credential_id)
        except Exception as e:
            return {
                "valid": False,
                "error": str(e),
                "error_type": "CREDENTIAL_VALIDATION_ERROR",
            }

    async def _validate_configuration(
        self, instance: ToolInstance, report: Dict[str, Any]
    ):
        """Validate tool configuration"""
        try:
            # Get tool definition for configuration schema
            tool_definition = await self.tool_registry_service.get_tool(
                instance.tool_definition_id
            )
            if not tool_definition:
                report["validations"]["configuration"]["status"] = "skipped"
                return

            # Basic configuration validation
            if tool_definition.configuration_schema:
                # TODO: Implement JSON schema validation
                # For now, just check that configuration exists if schema is defined
                if not instance.configuration:
                    report["warnings"].append(
                        {
                            "type": "CONFIGURATION_EMPTY",
                            "message": "Tool configuration is empty but schema is defined",
                            "severity": "medium",
                        }
                    )

            report["validations"]["configuration"]["status"] = "passed"

        except Exception as e:
            report["validations"]["configuration"]["status"] = "error"
            report["errors"].append(
                {
                    "type": "CONFIGURATION_VALIDATION_ERROR",
                    "message": f"Error validating configuration: {str(e)}",
                    "severity": "medium",
                }
            )

    # ================== USER MESSAGE HELPERS ==================

    def _get_credential_not_configured_message(
        self, cred_type: str, tool_name: str
    ) -> str:
        """Get user-friendly message for missing credential configuration"""
        messages = {
            "OAUTH_TOKEN": f"{tool_name} requires access to your Google account, but it's not connected yet.",
            "API_KEY": f"{tool_name} requires an API key, but none is configured.",
            "CONNECTION_STRING": f"{tool_name} requires a database connection, but none is configured.",
            "USERNAME_PASSWORD": f"{tool_name} requires login credentials, but none are configured.",
        }
        return messages.get(
            cred_type,
            f"{tool_name} requires {cred_type} credentials, but none are configured.",
        )

    def _get_credential_invalid_message(
        self, cred_type: str, tool_name: str, error: str
    ) -> str:
        """Get user-friendly message for invalid credentials"""
        if cred_type == "OAUTH_TOKEN":
            if "expired" in str(error).lower():
                return f"Your Google account access for {tool_name} has expired and needs to be renewed."
            elif "scope" in str(error).lower():
                return f"Your Google account permissions for {tool_name} are insufficient. Additional permissions are needed."
            else:
                return f"There's an issue with your Google account connection for {tool_name}."
        else:
            return f"The {self._get_credential_display_name(cred_type)} for {tool_name} is invalid or expired."

    def _get_credential_fix_message(self, cred_type: str) -> str:
        """Get action message for fixing credentials"""
        messages = {
            "OAUTH_TOKEN": "Click the link below to reconnect your Google account",
            "API_KEY": "Please update your API key in the tool configuration",
            "CONNECTION_STRING": "Please verify and update your database connection settings",
            "USERNAME_PASSWORD": "Please update your login credentials",
        }
        return messages.get(cred_type, "Please update your credentials")

    def _get_credential_display_name(self, cred_type: str) -> str:
        """Get user-friendly display name for credential type"""
        names = {
            "OAUTH_TOKEN": "Google Account Connection",
            "API_KEY": "API Key",
            "CONNECTION_STRING": "Database Connection",
            "USERNAME_PASSWORD": "Login Credentials",
        }
        return names.get(cred_type, cred_type.replace("_", " ").title())
