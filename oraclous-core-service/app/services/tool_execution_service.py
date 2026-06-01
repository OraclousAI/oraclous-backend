# app/services/tool_execution_service.py
import asyncio
import logging
import uuid as _uuid
from typing import Dict, Any, Optional, AsyncGenerator
from uuid import UUID, uuid4
from datetime import datetime

from app.services.instance_manager import InstanceManagerService
from app.services.credential_client import CredentialClient
from app.tools.factory import ToolFactory
from app.schemas.tool_instance import (
    ToolInstance,
    ExecutionContext,
    ExecutionResult,
    Execution,
    Job,
)
from app.schemas.common import InstanceStatus

logger = logging.getLogger(__name__)


class ToolExecutionService:
    """
    Unified service for tool execution - handles both sync and async execution,
    job tracking, progress monitoring, and tool capabilities
    """

    def __init__(
        self,
        instance_manager: InstanceManagerService,
        credential_client: CredentialClient,
        validation_service: Optional["ValidationService"] = None,
    ):
        self.instance_manager = instance_manager
        self.credential_client = credential_client
        self.validation_service = validation_service

        # In-memory job storage (replace with Redis/DB in production)
        self.active_jobs: Dict[str, Dict[str, Any]] = {}
        self.job_results: Dict[str, Dict[str, Any]] = {}

    # ================== SYNCHRONOUS EXECUTION ==================

    async def execute_sync(
        self,
        instance_id: str,
        user_id: UUID,
        input_data: Dict[str, Any],
        max_retries: int = 3,
    ) -> ExecutionResult:
        """
        Execute a tool instance synchronously (for quick operations)
        Returns execution result immediately
        """
        try:
            # 1. Validate instance readiness
            validation_result = await self._validate_execution_readiness(
                instance_id, user_id
            )
            if not validation_result["is_ready"]:
                return ExecutionResult(
                    success=False,
                    error_message=validation_result["error_message"],
                    error_type="VALIDATION_FAILED",
                    metadata=validation_result.get("metadata", {}),
                )

            instance = validation_result["instance"]

            # 2. Create execution record
            execution = await self.instance_manager.create_execution(
                instance_id=instance_id,
                user_id=user_id,
                input_data=input_data,
                max_retries=max_retries,
            )

            # 3. Execute directly
            start_time = datetime.utcnow()

            try:
                # Update execution to RUNNING
                await self.instance_manager.repo.update_execution(
                    execution.id, {"status": "RUNNING", "started_at": start_time}
                )

                # Build context and execute
                context = await self._build_execution_context(
                    instance, execution, user_id
                )

                result = await self._execute_tool(instance, input_data, context)

                # Calculate processing time
                processing_time = int(
                    (datetime.utcnow() - start_time).total_seconds() * 1000
                )
                result.processing_time_ms = processing_time

                # Update records
                await self._update_execution_with_result(str(execution.id), result)
                await self._update_instance_stats(instance, result, str(execution.id))

                logger.info(f"Sync execution completed for instance {instance_id}")
                return result

            except Exception as e:
                error_result = ExecutionResult(
                    success=False,
                    error_message=str(e),
                    error_type=type(e).__name__,
                    processing_time_ms=int(
                        (datetime.utcnow() - start_time).total_seconds() * 1000
                    ),
                )

                await self._update_execution_with_result(execution.id, error_result)
                return error_result

        except Exception as e:
            logger.error(f"Sync execution failed for instance {instance_id}: {str(e)}")
            return ExecutionResult(
                success=False,
                error_message=f"Execution service error: {str(e)}",
                error_type="EXECUTION_SERVICE_ERROR",
            )

    # ================== ASYNCHRONOUS EXECUTION ==================

    async def execute_async(
        self,
        instance_id: str,
        user_id: UUID,
        input_data: Dict[str, Any],
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        """
        Execute a tool instance asynchronously (for long-running operations)
        Returns job information for tracking
        """
        try:
            # 1. Validate instance readiness
            validation_result = await self._validate_execution_readiness(
                instance_id, user_id
            )
            if not validation_result["is_ready"]:
                raise ValueError(validation_result["error_message"])

            instance = validation_result["instance"]

            # 2. Create execution record
            execution = await self.instance_manager.create_execution(
                instance_id=instance_id,
                user_id=user_id,
                input_data=input_data,
                max_retries=max_retries,
            )

            # 3. Create job record
            job_id = str(uuid4())
            job = Job(
                id=job_id,
                job_type="tool_execution",
                execution_id=execution.id,
                queue_name="default",
                priority=0,
                status="QUEUED",
                job_data={
                    "instance_id": instance_id,
                    "user_id": str(user_id),
                    "input_data": input_data,
                    "max_retries": max_retries,
                },
                scheduled_at=datetime.utcnow(),
            )

            # 4. Store job in repository
            await self.instance_manager.repo.create_job(job)

            # 5. Add to active jobs for processing
            self.active_jobs[job_id] = {
                "job": job,
                "execution": execution,
                "instance": instance,
                "status": "QUEUED",
                "progress": 0,
                "current_step": None,
                "created_at": datetime.utcnow(),
            }

            # 6. Start processing (fire and forget)
            asyncio.create_task(self._process_async_job(job_id))

            return {
                "job_id": job_id,
                "execution_id": execution.id,
                "status": "QUEUED",
                "estimated_duration": self._estimate_execution_duration(instance),
                "progress_url": f"/api/v1/instances/{instance_id}/jobs/{job_id}/progress",
                "result_url": f"/api/v1/instances/{instance_id}/jobs/{job_id}/result",
            }

        except Exception as e:
            logger.error(f"Failed to submit async execution job: {str(e)}")
            raise

    # ================== JOB MANAGEMENT ==================

    async def get_job_progress(self, job_id: str) -> Dict[str, Any]:
        """Get current progress of a job"""
        if job_id in self.active_jobs:
            job_info = self.active_jobs[job_id]
            return {
                "job_id": job_id,
                "status": job_info["status"],
                "progress": job_info["progress"],
                "current_step": job_info["current_step"],
                "started_at": job_info.get("started_at"),
                "estimated_completion": job_info.get("estimated_completion"),
                "error_message": job_info.get("error_message"),
            }
        elif job_id in self.job_results:
            result_info = self.job_results[job_id]
            return {
                "job_id": job_id,
                "status": result_info["status"],
                "progress": 100 if result_info["status"] == "COMPLETED" else 0,
                "completed_at": result_info.get("completed_at"),
                "error_message": result_info.get("error_message"),
            }
        else:
            raise ValueError(f"Job {job_id} not found")

    async def get_job_result(self, job_id: str) -> Dict[str, Any]:
        """Get final result of a completed job"""
        if job_id in self.job_results:
            return self.job_results[job_id]
        elif job_id in self.active_jobs:
            job_info = self.active_jobs[job_id]
            return {
                "job_id": job_id,
                "status": job_info["status"],
                "message": "Job still processing",
            }
        else:
            raise ValueError(f"Job {job_id} not found")

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a running job"""
        if job_id in self.active_jobs:
            job_info = self.active_jobs[job_id]
            if job_info["status"] in ["QUEUED", "RUNNING"]:
                job_info["status"] = "CANCELLED"
                job_info["cancelled_at"] = datetime.utcnow()

                # Update execution record
                await self.instance_manager.repo.update_execution(
                    job_info["execution"].id,
                    {
                        "status": "CANCELLED",
                        "completed_at": datetime.utcnow(),
                        "error_message": "Job cancelled by user",
                    },
                )

                # Update job record
                await self.instance_manager.repo.update_job(
                    job_id, {"status": "CANCELLED", "completed_at": datetime.utcnow()}
                )

                logger.info(f"Job {job_id} cancelled successfully")
                return True
            else:
                return False
        else:
            return False

    async def stream_job_progress(
        self, job_id: str
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Stream job progress updates (for WebSocket or SSE)"""
        last_progress = -1
        last_status = None

        while True:
            try:
                progress_info = await self.get_job_progress(job_id)

                # Yield update if progress or status changed
                if (
                    progress_info["progress"] != last_progress
                    or progress_info["status"] != last_status
                ):
                    yield progress_info
                    last_progress = progress_info["progress"]
                    last_status = progress_info["status"]

                # Break if job is completed or failed
                if progress_info["status"] in ["COMPLETED", "FAILED", "CANCELLED"]:
                    break

                # Wait before next check
                await asyncio.sleep(1)

            except ValueError:
                break
            except Exception as e:
                logger.error(f"Error streaming progress for job {job_id}: {str(e)}")
                yield {"job_id": job_id, "status": "ERROR", "error_message": str(e)}
                break

    # ================== TOOL CAPABILITIES ==================

    async def get_tool_capabilities(self, tool_definition_id: str) -> Dict[str, Any]:
        """Get capabilities and metadata for a tool definition"""
        try:
            # Get tool definition from registry
            tool_definition = await self.instance_manager.tool_registry.get_tool_definition(
                _uuid.UUID(tool_definition_id)
            )
            if not tool_definition:
                return {"error": "Tool definition not found", "capabilities": []}

            # Check implementation availability
            try:
                executor = ToolFactory.create_executor(tool_definition_id)
                implementation_available = True
                implementation_type = executor.__class__.__name__
            except Exception:
                implementation_available = False
                implementation_type = None

            return {
                "tool_definition": {
                    "id": tool_definition.id,
                    "name": tool_definition.name,
                    "description": tool_definition.description,
                    "version": tool_definition.version,
                    "category": tool_definition.category,
                    "type": tool_definition.type,
                },
                "capabilities": [
                    {
                        "name": cap.name,
                        "description": cap.description,
                        "parameters": cap.parameters,
                    }
                    for cap in tool_definition.capabilities
                ],
                "input_schema": tool_definition.input_schema,
                "output_schema": tool_definition.output_schema,
                "configuration_schema": tool_definition.configuration_schema,
                "credential_requirements": [
                    {
                        "type": req.type.value,
                        "required": req.required,
                        "scopes": req.scopes,
                        "description": req.description,
                    }
                    for req in tool_definition.credential_requirements
                ],
                "implementation": {
                    "available": implementation_available,
                    "type": implementation_type,
                },
            }

        except Exception as e:
            logger.error(
                f"Failed to get tool capabilities for {tool_definition_id}: {str(e)}"
            )
            return {"error": str(e), "capabilities": []}

    async def list_available_tools(self) -> Dict[str, Any]:
        """List all available tools with their capabilities.

        Requires org context to enumerate capabilities — callers that need
        a scoped list should call CapabilityRegistryService.list_by_org() directly.
        """
        return {"tools": [], "total": 0}

    # ================== INTERNAL METHODS ==================

    async def _validate_execution_readiness(
        self, instance_id: str, user_id: UUID
    ) -> Dict[str, Any]:
        """Validate if instance is ready for execution"""
        instance = await self.instance_manager.get_user_instance(instance_id, user_id)
        if not instance:
            return {
                "is_ready": False,
                "error_message": "Tool instance not found",
                "metadata": {"error_type": "INSTANCE_NOT_FOUND"},
            }

        # Enhanced validation if validation service is available
        if self.validation_service:
            validation_report = (
                await self.validation_service.validate_execution_readiness(
                    instance, user_id
                )
            )

            if not validation_report["is_ready"]:
                error_messages = [
                    error["message"] for error in validation_report["errors"]
                ]
                action_items = [
                    {
                        "action": action["action"],
                        "message": action["message"],
                        "url": action.get("url"),
                        "priority": action["priority"],
                    }
                    for action in validation_report["action_items"]
                ]

                return {
                    "is_ready": False,
                    "error_message": "; ".join(error_messages),
                    "metadata": {
                        "validation_report": validation_report,
                        "action_items": action_items,
                    },
                }
        else:
            # Fallback to basic status check
            if instance.status != InstanceStatus.READY:
                return {
                    "is_ready": False,
                    "error_message": f"Tool instance not ready for execution. Status: {instance.status}",
                    "metadata": {"error_type": "INSTANCE_NOT_READY"},
                }

        return {"is_ready": True, "instance": instance}

    async def _process_async_job(self, job_id: str):
        """Internal method to process a job asynchronously"""
        job_info = self.active_jobs.get(job_id)
        if not job_info:
            logger.error(f"Job {job_id} not found in active jobs")
            return

        try:
            job = job_info["job"]
            execution = job_info["execution"]
            instance = job_info["instance"]

            # Update status to running
            job_info["status"] = "RUNNING"
            job_info["started_at"] = datetime.utcnow()
            job_info["progress"] = 10
            job_info["current_step"] = "Initializing execution"

            await self._update_job_status(
                job_id, "RUNNING", {"started_at": datetime.utcnow()}
            )
            await self._update_execution_status(
                execution.id, "RUNNING", {"started_at": datetime.utcnow()}
            )

            # Build execution context
            job_info["current_step"] = "Resolving credentials"
            job_info["progress"] = 20

            context = await self._build_execution_context(
                instance, execution, UUID(job.job_data["user_id"])
            )

            # Execute tool with progress tracking
            job_info["current_step"] = "Executing tool"
            job_info["progress"] = 30

            result = await self._execute_tool_with_progress(
                job_id, instance, job.job_data["input_data"], context
            )

            # Store result
            job_info["progress"] = 100
            job_info["current_step"] = "Completed"

            # Move to completed jobs
            self.job_results[job_id] = {
                "job_id": job_id,
                "execution_id": execution.id,
                "status": "COMPLETED" if result.success else "FAILED",
                "result": {
                    "success": result.success,
                    "data": result.data,
                    "error_message": result.error_message,
                    "error_type": result.error_type,
                    "credits_consumed": float(result.credits_consumed),
                    "processing_time_ms": result.processing_time_ms,
                    "metadata": result.metadata,
                },
                "completed_at": datetime.utcnow(),
            }

            # Update records
            await self._update_execution_with_result(execution.id, result)
            await self._update_job_status(
                job_id,
                "COMPLETED" if result.success else "FAILED",
                {
                    "completed_at": datetime.utcnow(),
                    "result_data": self.job_results[job_id]["result"],
                },
            )

            # Clean up active job
            del self.active_jobs[job_id]

            logger.info(f"Job {job_id} completed successfully")

        except Exception as e:
            logger.error(f"Job {job_id} failed with error: {str(e)}")

            # Handle job failure
            job_info["status"] = "FAILED"
            job_info["error_message"] = str(e)
            job_info["completed_at"] = datetime.utcnow()

            # Store failed result
            self.job_results[job_id] = {
                "job_id": job_id,
                "execution_id": execution.id,
                "status": "FAILED",
                "error_message": str(e),
                "error_type": type(e).__name__,
                "completed_at": datetime.utcnow(),
            }

            # Update records
            await self._update_execution_status(
                execution.id,
                "FAILED",
                {
                    "completed_at": datetime.utcnow(),
                    "error_message": str(e),
                    "error_type": type(e).__name__,
                },
            )
            await self._update_job_status(
                job_id, "FAILED", {"completed_at": datetime.utcnow()}
            )

            # Clean up active job
            del self.active_jobs[job_id]

    async def _execute_tool(
        self,
        instance: ToolInstance,
        input_data: Dict[str, Any],
        context: ExecutionContext,
    ) -> ExecutionResult:
        """Execute tool directly"""
        return await ToolFactory.execute_tool(instance, input_data, context)

    async def _execute_tool_with_progress(
        self,
        job_id: str,
        instance: ToolInstance,
        input_data: Dict[str, Any],
        context: ExecutionContext,
    ) -> ExecutionResult:
        """Execute tool with progress updates"""
        job_info = self.active_jobs.get(job_id)

        try:
            # Update progress
            if job_info:
                job_info["progress"] = 40
                job_info["current_step"] = "Starting tool execution"

            # Execute
            result = await self._execute_tool(instance, input_data, context)

            # Update progress
            if job_info:
                job_info["progress"] = 90
                job_info["current_step"] = "Finalizing results"

            return result

        except Exception as e:
            logger.error(f"Tool execution failed for job {job_id}: {str(e)}")
            raise

    async def _build_execution_context(
        self, instance: ToolInstance, execution: Execution, user_id: UUID
    ) -> ExecutionContext:
        """Build execution context with resolved credentials"""
        credentials = {}

        if instance.credential_mappings:
            for cred_type, cred_identifier in instance.credential_mappings.items():
                if cred_type == "OAUTH_TOKEN":
                    token_data = await self.credential_client.get_runtime_token(
                        user_id=user_id, provider=cred_identifier
                    )
                    if token_data:
                        credentials[cred_type] = token_data
                else:
                    cred_data = await self.credential_client._get_credential_data(
                        cred_identifier
                    )
                    if cred_data:
                        credentials[cred_type] = cred_data

        return ExecutionContext(
            instance_id=instance.id,
            workflow_id=str(instance.workflow_id),
            user_id=str(user_id),
            job_id=execution.id,
            credentials=credentials,
            configuration=instance.configuration,
            settings=instance.settings,
        )

    async def _update_execution_status(
        self, execution_id: str, status: str, updates: Dict[str, Any]
    ):
        """Update execution record"""
        updates["status"] = status
        await self.instance_manager.repo.update_execution(execution_id, updates)

    async def _update_job_status(
        self, job_id: str, status: str, updates: Dict[str, Any]
    ):
        """Update job record"""
        updates["status"] = status
        await self.instance_manager.repo.update_job(job_id, updates)

    async def _update_execution_with_result(
        self, execution_id: str, result: ExecutionResult
    ):
        """Update execution with final result"""
        update_data = {
            "status": "SUCCESS" if result.success else "FAILED",
            "completed_at": datetime.utcnow(),
            "credits_consumed": result.credits_consumed,
            "processing_time_ms": result.processing_time_ms,
        }

        if result.success:
            update_data["output_data"] = result.data
            update_data["execution_metadata"] = result.metadata
        else:
            update_data["error_message"] = result.error_message
            update_data["error_type"] = result.error_type

        await self.instance_manager.repo.update_execution(execution_id, update_data)

    async def _update_instance_stats(
        self, instance: ToolInstance, result: ExecutionResult, execution_id: str
    ):
        """Update instance statistics using dedicated repository method"""
        success = await self.instance_manager.repo.update_instance_execution_stats(
            instance_id=str(instance.id),
            execution_id=execution_id,
            credits_consumed=result.credits_consumed,
            execution_count_increment=1,
        )

        if not success:
            logger.warning(f"Failed to update stats for instance {instance.id}")

    def _estimate_execution_duration(self, instance: ToolInstance) -> Optional[int]:
        """Estimate execution duration based on tool type and history"""
        tool_estimates = {
            "GoogleDriveReader": 30,
            "PostgreSQLReader": 15,
            "MySQLReader": 15,
            "NotionReader": 45,
        }
        return tool_estimates.get("default", 60)
