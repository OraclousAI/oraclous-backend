from fastapi import APIRouter, Depends, HTTPException, Query, Path
from fastapi.security import OAuth2PasswordBearer
from typing import List, Optional, Dict, Any
from uuid import UUID
from app.schemas.tool_instance import (
    ToolInstance,
    CreateInstanceRequest,
    UpdateInstanceRequest,
    ConfigureCredentialsRequest,
    InstanceStatusResponse,
    InstanceCredentialStatus,
    InstanceListResponse,
    Execution,
)
from app.schemas.common import InstanceStatus
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.instance_manager import InstanceManagerService
from app.repositories.instance_repository import InstanceRepository
from app.services.capability_registry import CapabilityRegistryService
from app.services.credential_client import CredentialClient
from app.services.tool_execution_service import ToolExecutionService
from app.services.validation_service import ValidationService
from fastapi.responses import StreamingResponse
from app.core.database import get_session
import json

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
router = APIRouter()


# Dependency to get current user ID
async def get_current_user_id() -> UUID:
    # TODO: Implement proper JWT token validation
    # For now, mock a user ID - replace with actual auth integration
    return UUID("af8531b1-4459-4599-8f70-a438dfca741d")


# Dependency to get instance service
async def get_instance_service(
    db: AsyncSession = Depends(get_session),
) -> InstanceManagerService:
    instance_repo = InstanceRepository(db)
    capability_registry = CapabilityRegistryService(db)
    credential_client = CredentialClient()

    return InstanceManagerService(
        instance_repo=instance_repo,
        tool_registry=capability_registry,
        credential_client=credential_client,
    )


async def get_execution_service(
    instance_service: InstanceManagerService = Depends(get_instance_service),
) -> ToolExecutionService:
    credential_client = CredentialClient()

    # Optionally include validation service
    validation_service = ValidationService(
        credential_client=credential_client,
        tool_registry_service=instance_service.tool_registry,
    )

    return ToolExecutionService(
        instance_manager=instance_service,
        credential_client=credential_client,
        validation_service=validation_service,
    )


async def get_validation_service(
    service: InstanceManagerService = Depends(get_instance_service),
) -> ValidationService:
    credential_client = CredentialClient()
    return ValidationService(
        credential_client=credential_client, tool_registry_service=service.tool_registry
    )


# Simple endpoint to refresh instance credentials and status
@router.post(
    "/{instance_id}/refresh-credentials", response_model=InstanceStatusResponse
)
async def refresh_instance_credentials(
    instance_id: str,
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
):
    """
    Refresh credentials for an instance and update status if new token is available.
    Handles edge case: if credential_mappings is missing, auto-generate from tool definition.
    """
    try:
        instance = await service.get_user_instance(instance_id, user_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")
        tool_definition = await service.tool_registry.get_tool(
            instance.tool_definition_id
        )
        required_scopes = service._build_required_scopes_map(tool_definition)
        # Auto-generate credential_mappings if missing
        credential_mappings = instance.credential_mappings or {}
        if not credential_mappings:
            for cred_req in tool_definition.credential_requirements:
                cred_type = cred_req.type.value
                # Use provider from tool definition if available, else fallback to cred_type
                provider = (
                    getattr(cred_req, "provider", None) or "google"
                    if cred_type == "OAUTH_TOKEN"
                    else cred_type
                )
                credential_mappings[cred_type] = provider
            # Persist this mapping to the instance
            await service.repo.configure_credentials(
                instance_id, user_id, credential_mappings
            )
            instance.credential_mappings = credential_mappings
        credential_validation = await service.credential_client.validate_credentials(
            user_id=user_id,
            credential_mappings=credential_mappings,
            required_scopes=required_scopes,
        )
        all_valid = all(v.get("valid", False) for v in credential_validation.values())
        if all_valid:
            await service.repo.update_instance_status(instance_id, InstanceStatus.READY)
            instance.status = InstanceStatus.READY
        else:
            await service.repo.update_instance_status(
                instance_id, InstanceStatus.CONFIGURATION_REQUIRED
            )
            instance.status = InstanceStatus.CONFIGURATION_REQUIRED

        # Build credentials_status list from credential_validation and tool_definition
        credentials_status = []
        for cred_req in tool_definition.credential_requirements:
            cred_type = cred_req.type.value
            cred_validation = credential_validation.get(cred_type, {})
            credentials_status.append(
                InstanceCredentialStatus(
                    credential_type=cred_type,
                    required=getattr(cred_req, "required", True),
                    configured=cred_validation.get("configured", True),
                    valid=cred_validation.get("valid", False),
                    error_message=cred_validation.get("error"),
                    login_url=cred_validation.get("login_url"),
                )
            )
        return InstanceStatusResponse(
            instance=instance,
            credentials_status=credentials_status,
            is_ready_for_execution=all_valid,
            validation_errors=[],
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to refresh credentials: {str(e)}"
        )


@router.post("/", response_model=ToolInstance, status_code=201)
async def create_instance(
    request: CreateInstanceRequest,
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
):
    """Create a new tool instance"""
    try:
        instance = await service.create_instance(
            user_id=user_id,
            tool_definition_id=request.tool_definition_id,
            workflow_id=request.workflow_id,
            configuration=request.configuration,
        )
        # Return instance plus credential info
        return {
            **instance.dict(),
            "oauth_redirects": getattr(instance, "oauth_redirects", {}),
            "missing_credentials": getattr(instance, "missing_credentials", []),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to create instance: {str(e)}"
        )


@router.get("/{instance_id}", response_model=ToolInstance)
async def get_instance(
    instance_id: str = Path(..., description="Instance ID"),
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
):
    """Get a specific tool instance"""
    instance = await service.get_user_instance(instance_id, user_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    return instance


@router.put("/{instance_id}", response_model=ToolInstance)
async def update_instance(
    instance_id: str,
    request: UpdateInstanceRequest,
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
):
    """Update tool instance configuration"""
    try:
        # Update using the repository directly for now
        updated_instance = await service.repo.update_instance(
            instance_id, user_id, request
        )
        if not updated_instance:
            raise HTTPException(status_code=404, detail="Instance not found")
        return updated_instance
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to update instance: {str(e)}"
        )


@router.post(
    "/{instance_id}/configure-credentials", response_model=InstanceStatusResponse
)
async def configure_instance_credentials(
    instance_id: str,
    request: ConfigureCredentialsRequest,
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
):
    """Configure credentials for a tool instance"""
    try:
        status_response = await service.configure_credentials(
            instance_id, user_id, request
        )
        # Return status response plus credential info
        return {
            **status_response.dict(),
            "oauth_redirects": getattr(status_response.instance, "oauth_redirects", {}),
            "missing_credentials": getattr(
                status_response.instance, "missing_credentials", []
            ),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to configure credentials: {str(e)}"
        )


@router.get("/{instance_id}/status", response_model=InstanceStatusResponse)
async def get_instance_status(
    instance_id: str,
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
):
    """Get complete status information for an instance"""
    try:
        status_response = await service.get_instance_status(instance_id, user_id)
        return status_response
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get instance status: {str(e)}"
        )


@router.post("/{instance_id}/execute-async", response_model=dict)
async def execute_tool_instance_async(
    instance_id: str,
    input_data: Dict[str, Any],
    max_retries: int = Query(3, ge=0, le=10),
    user_id: UUID = Depends(get_current_user_id),
    execution_service: ToolExecutionService = Depends(get_execution_service),
):
    """
    Execute tool instance asynchronously for long-running tasks
    Returns job information for tracking progress
    """
    try:
        job_info = await execution_service.execute_async(
            instance_id=instance_id,
            user_id=user_id,
            input_data=input_data,
            max_retries=max_retries,
        )

        return job_info

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to submit execution job: {str(e)}"
        )


@router.get("/{instance_id}/jobs/{job_id}/progress", response_model=dict)
async def get_job_progress(
    instance_id: str,
    job_id: str,
    user_id: UUID = Depends(get_current_user_id),
    execution_service: ToolExecutionService = Depends(get_execution_service),
):
    """Get current progress of an execution job"""
    try:
        # Verify user owns this instance (security check)
        instance = await execution_service.instance_manager.get_user_instance(
            instance_id, user_id
        )
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")

        progress = await execution_service.get_job_progress(job_id)
        return progress

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get job progress: {str(e)}"
        )


@router.get("/{instance_id}/jobs/{job_id}/result", response_model=dict)
async def get_job_result(
    instance_id: str,
    job_id: str,
    user_id: UUID = Depends(get_current_user_id),
    execution_service: ToolExecutionService = Depends(get_execution_service),
):
    """Get final result of a completed execution job"""
    try:
        # Verify user owns this instance
        instance = await execution_service.instance_manager.get_user_instance(
            instance_id, user_id
        )
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")

        result = await execution_service.get_job_result(job_id)
        return result

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{instance_id}/jobs/{job_id}/cancel", response_model=dict)
async def cancel_execution_job(
    instance_id: str,
    job_id: str,
    user_id: UUID = Depends(get_current_user_id),
    execution_service: ToolExecutionService = Depends(get_execution_service),
):
    """
    Cancel a running execution job
    """
    try:
        # Verify user owns this instance
        instance = await execution_service.instance_manager.get_user_instance(
            instance_id, user_id
        )
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")

        success = await execution_service.cancel_job(job_id)

        if success:
            return {"message": "Job cancelled successfully", "job_id": job_id}
        else:
            raise HTTPException(status_code=400, detail="Job could not be cancelled")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to cancel job: {str(e)}")


@router.get("/{instance_id}/jobs/{job_id}/stream")
async def stream_job_progress(
    instance_id: str,
    job_id: str,
    user_id: UUID = Depends(get_current_user_id),
    execution_service: ToolExecutionService = Depends(get_execution_service),
):
    """
    Stream job progress updates (Server-Sent Events)
    """
    try:
        # Verify user owns this instance
        instance = await execution_service.instance_manager.get_user_instance(
            instance_id, user_id
        )
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")

        async def generate_progress_stream():
            """Generate SSE stream of progress updates"""
            try:
                async for progress_update in execution_service.stream_job_progress(
                    job_id
                ):
                    # Format as Server-Sent Event
                    data = json.dumps(progress_update)
                    yield f"data: {data}\n\n"

                # Send completion event
                yield f"data: {json.dumps({'status': 'stream_ended'})}\n\n"

            except Exception as e:
                error_data = json.dumps({"status": "error", "error_message": str(e)})
                yield f"data: {error_data}\n\n"

        return StreamingResponse(
            generate_progress_stream(),
            media_type="text/plain",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "text/event-stream",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to stream progress: {str(e)}"
        )


@router.get("/", response_model=InstanceListResponse)
async def list_instances(
    workflow_id: Optional[str] = Query(None, description="Filter by workflow ID"),
    status: Optional[InstanceStatus] = Query(None, description="Filter by status"),
    page: int = Query(0, ge=0, description="Page number"),
    size: int = Query(50, ge=1, le=100, description="Page size"),
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
):
    """List user's tool instances with filtering and pagination"""
    try:
        instances, total = await service.list_user_instances(
            user_id=user_id,
            workflow_id=workflow_id,
            status=status,
            page=page,
            size=size,
        )

        return InstanceListResponse(
            instances=instances, total=total, page=page, size=size
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to list instances: {str(e)}"
        )


@router.delete("/{instance_id}", status_code=204)
async def delete_instance(
    instance_id: str,
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
):
    """Delete a tool instance"""
    try:
        success = await service.delete_instance(instance_id, user_id)
        if not success:
            raise HTTPException(status_code=404, detail="Instance not found")
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to delete instance: {str(e)}"
        )


@router.post("/{instance_id}/execute-sync", response_model=dict)
async def execute_tool_instance_sync(
    instance_id: str,
    input_data: Dict[str, Any],
    max_retries: int = Query(3, ge=0, le=10),
    user_id: UUID = Depends(get_current_user_id),
    execution_service: ToolExecutionService = Depends(get_execution_service),
):
    """
    Execute tool instance synchronously and return result immediately
    Use this for quick operations or testing
    """
    try:
        result = await execution_service.execute_sync(
            instance_id=instance_id,
            user_id=user_id,
            input_data=input_data,
            max_retries=max_retries,
        )

        return {
            "success": result.success,
            "data": result.data,
            "error_message": result.error_message,
            "error_type": result.error_type,
            "credits_consumed": float(result.credits_consumed),
            "processing_time_ms": result.processing_time_ms,
            "metadata": result.metadata,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Execution failed: {str(e)}")


@router.get("/{instance_id}/executions", response_model=List[Execution])
async def list_instance_executions(
    instance_id: str,
    page: int = Query(0, ge=0, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size"),
    status: Optional[str] = Query(None, description="Filter by execution status"),
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
):
    """List executions for a specific instance"""
    try:
        # First verify user owns this instance
        instance = await service.get_user_instance(instance_id, user_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")

        executions, total = await service.repo.list_executions(
            user_id=user_id,
            instance_id=instance_id,
            status=status,
            page=page,
            size=size,
        )

        return executions
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to list executions: {str(e)}"
        )


# Additional utility endpoints


@router.get("/{instance_id}/validate", response_model=dict)
async def validate_instance_ready(
    instance_id: str,
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
):
    """Check if instance is ready for execution"""
    try:
        # Verify user owns this instance
        instance = await service.get_user_instance(instance_id, user_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")

        is_ready = await service.validate_instance_ready(instance_id)

        return {
            "instance_id": instance_id,
            "is_ready": is_ready,
            "status": instance.status.value,
            "message": "Instance is ready for execution"
            if is_ready
            else "Instance requires configuration",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to validate instance: {str(e)}"
        )


@router.get("/workflow/{workflow_id}/instances", response_model=List[ToolInstance])
async def list_workflow_instances(
    workflow_id: str,
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
):
    """Get all instances for a specific workflow"""
    try:
        # Filter by user to ensure they only see their own instances
        instances, _ = await service.list_user_instances(
            user_id=user_id,
            workflow_id=workflow_id,
            size=1000,  # Large limit to get all instances for the workflow
        )

        return instances
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to list workflow instances: {str(e)}"
        )


# Credential-related helper endpoints


@router.get("/{instance_id}/available-credentials", response_model=dict)
async def get_available_credentials(
    instance_id: str,
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
):
    """Get available credentials/data sources for the instance's tool"""
    try:
        # Get instance and tool definition
        instance = await service.get_user_instance(instance_id, user_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")

        tool_definition = await service.tool_registry.get_tool(
            instance.tool_definition_id
        )
        if not tool_definition:
            raise HTTPException(status_code=404, detail="Tool definition not found")

        # Get available data sources from credential client
        available_sources = await service.credential_client.get_available_data_sources(
            user_id
        )

        # Filter based on tool requirements
        relevant_sources = {}
        for cred_req in tool_definition.credential_requirements:
            if cred_req.type.value == "OAUTH_TOKEN":
                # For OAuth, show available providers
                for provider, sources in available_sources.items():
                    if sources:  # Only show providers with available sources
                        relevant_sources[provider] = sources

        return {
            "instance_id": instance_id,
            "tool_name": tool_definition.name,
            "required_credentials": [
                req.type.value
                for req in tool_definition.credential_requirements
                if req.required
            ],
            "optional_credentials": [
                req.type.value
                for req in tool_definition.credential_requirements
                if not req.required
            ],
            "available_data_sources": relevant_sources,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get available credentials: {str(e)}"
        )


@router.get("/{instance_id}/validate-execution", response_model=dict)
async def validate_execution_readiness(
    instance_id: str,
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
    validation_service: ValidationService = Depends(get_validation_service),
):
    """
    Comprehensive validation of tool instance readiness for execution
    Returns detailed report with user-friendly error messages and action items
    """
    try:
        # Get instance
        instance = await service.get_user_instance(instance_id, user_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")

        # Run comprehensive validation
        validation_report = await validation_service.validate_execution_readiness(
            instance, user_id
        )

        return validation_report

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Validation failed: {str(e)}")


@router.get("/{instance_id}/health", response_model=dict)
async def get_instance_health(
    instance_id: str,
    user_id: UUID = Depends(get_current_user_id),
    service: InstanceManagerService = Depends(get_instance_service),
    validation_service: ValidationService = Depends(get_validation_service),
):
    """
    Get instance health status with simplified success/failure and action items
    Useful for dashboards and quick status checks
    """
    try:
        instance = await service.get_user_instance(instance_id, user_id)
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")

        validation_report = await validation_service.validate_execution_readiness(
            instance, user_id
        )

        # Simplify the report for health check
        health_status = {
            "instance_id": instance_id,
            "is_healthy": validation_report["is_ready"],
            "status": instance.status.value,
            "summary": {
                "tool_available": validation_report["validations"]["implementation"][
                    "status"
                ]
                == "passed",
                "credentials_valid": validation_report["validations"]["credentials"][
                    "status"
                ]
                == "passed",
                "configuration_valid": validation_report["validations"][
                    "configuration"
                ]["status"]
                == "passed",
            },
            "issues_count": len(validation_report["errors"]),
            "warnings_count": len(validation_report["warnings"]),
            "action_items": validation_report["action_items"][
                :3
            ],  # Limit to top 3 actions
        }

        # Add quick fix suggestions
        if not health_status["is_healthy"]:
            critical_errors = [
                e
                for e in validation_report["errors"]
                if e.get("severity") == "critical"
            ]
            if critical_errors:
                health_status["primary_issue"] = critical_errors[0]["message"]

        return health_status

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Health check failed: {str(e)}")
