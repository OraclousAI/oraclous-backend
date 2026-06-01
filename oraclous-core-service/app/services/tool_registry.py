from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func
from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime, timezone

from app.interfaces.tool_registry import BaseToolRegistry
from app.schemas.tool_definition import ToolDefinition, ToolQuery
from app.schemas.common import ToolCategory
from app.models.tool_definition import ToolDefinitionDB
from app.utils.validation import ToolValidationMixin


class ToolRegistryService(BaseToolRegistry, ToolValidationMixin):
    def __init__(self, db_session: AsyncSession):
        self.db = db_session

    async def register_tool(self, definition: ToolDefinition) -> bool:
        """Register a new tool definition"""
        try:
            # Validate the definition
            if not self._validate_tool_definition(definition):
                return False

            # Convert Pydantic model to DB model
            db_tool = ToolDefinitionDB(
                id=definition.id,
                name=definition.name,
                description=definition.description,
                version=definition.version,
                icon=definition.icon,
                category=definition.category.value,
                type=definition.type.value,
                capabilities=[cap.dict() for cap in definition.capabilities],
                tags=definition.tags,
                input_schema=definition.input_schema.dict(),
                output_schema=definition.output_schema.dict(),
                configuration_schema=definition.configuration_schema.dict()
                if definition.configuration_schema
                else None,
                credential_requirements=[
                    req.dict() for req in definition.credential_requirements
                ],
                dependencies=definition.dependencies,
                author=definition.author,
                documentation_url=definition.documentation_url,
            )

            self.db.add(db_tool)
            await self.db.commit()
            await self.db.refresh(db_tool)
            return True

        except Exception as e:
            await self.db.rollback()
            print(f"Error registering tool: {e}")
            return False

    async def get_tool(self, tool_id: str) -> Optional[ToolDefinition]:
        """Retrieve tool definition by ID"""
        query = select(ToolDefinitionDB).where(ToolDefinitionDB.id == tool_id)
        result = await self.db.execute(query)
        db_tool = result.scalar_one_or_none()

        if not db_tool:
            return None

        return self._db_to_pydantic(db_tool)

    async def search_tools_advanced(self, query: ToolQuery) -> List[ToolDefinition]:
        """Search using ToolQuery model"""
        stmt = select(ToolDefinitionDB)
        conditions = []

        # Text search
        if query.text:
            search_condition = or_(
                ToolDefinitionDB.name.ilike(f"%{query.text}%"),
                ToolDefinitionDB.description.ilike(f"%{query.text}%"),
                ToolDefinitionDB.tags.op("@>")([query.text.lower()]),
            )
            conditions.append(search_condition)

        # Category filter
        if query.category:
            conditions.append(ToolDefinitionDB.category == query.category.value)

        # Type filter
        if query.type:
            conditions.append(ToolDefinitionDB.type == query.type.value)

        # Capabilities filter
        if query.capabilities:
            from sqlalchemy import cast
            from sqlalchemy.dialects.postgresql import JSONB

            for capability in query.capabilities:
                capability_jsonb = cast([{"name": capability}], JSONB)
                capability_condition = ToolDefinitionDB.capabilities.op("@>")(
                    capability_jsonb
                )
                conditions.append(capability_condition)

        # Tags filter
        if query.tags:
            for tag in query.tags:
                conditions.append(ToolDefinitionDB.tags.op("@>")([tag]))

        if conditions:
            stmt = stmt.where(and_(*conditions))

        # Add pagination
        stmt = stmt.offset(query.offset).limit(query.limit)
        stmt = stmt.order_by(ToolDefinitionDB.name)

        result = await self.db.execute(stmt)
        db_tools = result.scalars().all()

        return [self._db_to_pydantic(tool) for tool in db_tools]

    async def search_tools(
        self,
        query: str,
        category: Optional[ToolCategory] = None,
        capabilities: Optional[List[str]] = None,
    ) -> List[ToolDefinition]:
        """Search for tools by query and filters"""
        stmt = select(ToolDefinitionDB)

        conditions = []

        # Text search in name and description
        if query:
            search_condition = or_(
                ToolDefinitionDB.name.ilike(f"%{query}%"),
                ToolDefinitionDB.description.ilike(f"%{query}%"),
                ToolDefinitionDB.tags.op("@>")([query.lower()]),
            )
            conditions.append(search_condition)

        # Category filter
        if category:
            conditions.append(ToolDefinitionDB.category == category.value)

        # Capability filter (check if any required capability exists in tool capabilities)
        if capabilities:
            for capability in capabilities:
                capability_condition = ToolDefinitionDB.capabilities.op("@>")(
                    [{"name": capability}]
                )
                conditions.append(capability_condition)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        stmt = stmt.order_by(ToolDefinitionDB.name)

        result = await self.db.execute(stmt)
        db_tools = result.scalars().all()

        return [self._db_to_pydantic(tool) for tool in db_tools]

    async def match_capabilities(
        self, required_capabilities: List[str]
    ) -> List[ToolDefinition]:
        """Find tools that match required capabilities"""
        stmt = select(ToolDefinitionDB)

        # Build condition to check if tool has all required capabilities
        conditions = []
        for capability in required_capabilities:
            capability_condition = cast(ToolDefinitionDB.capabilities, JSONB).contains(
                cast(
                    func.jsonb_build_array(func.jsonb_build_object("name", capability)),
                    JSONB,
                )
            )
            conditions.append(capability_condition)

        if conditions:
            stmt = stmt.where(and_(*conditions))

        result = await self.db.execute(stmt)
        db_tools = result.scalars().all()

        return [self._db_to_pydantic(tool) for tool in db_tools]

    async def list_tools(
        self, category: Optional[ToolCategory] = None, limit: int = 50, offset: int = 0
    ) -> List[ToolDefinition]:
        """List tools with pagination"""
        stmt = select(ToolDefinitionDB)

        if category:
            print("Category:", category)
            stmt = stmt.where(ToolDefinitionDB.category == category.value)

        stmt = stmt.order_by(ToolDefinitionDB.name).offset(offset).limit(limit)
        result = await self.db.execute(stmt)
        db_tools = result.scalars().all()

        return [self._db_to_pydantic(tool) for tool in db_tools]

    async def update_tool(self, tool_id: str, definition: ToolDefinition) -> bool:
        """Update an existing tool definition"""
        try:
            query = select(ToolDefinitionDB).where(ToolDefinitionDB.id == tool_id)
            result = await self.db.execute(query)
            db_tool = result.scalar_one_or_none()

            if not db_tool:
                return False

            # Update fields
            db_tool.name = definition.name
            db_tool.description = definition.description
            db_tool.version = definition.version
            db_tool.icon = definition.icon
            db_tool.category = definition.category.value
            db_tool.type = definition.type.value
            db_tool.capabilities = [cap.dict() for cap in definition.capabilities]
            db_tool.tags = definition.tags
            db_tool.input_schema = definition.input_schema.dict()
            db_tool.output_schema = definition.output_schema.dict()
            db_tool.configuration_schema = (
                definition.configuration_schema.dict()
                if definition.configuration_schema
                else None
            )
            db_tool.credential_requirements = [
                req.dict() for req in definition.credential_requirements
            ]
            db_tool.dependencies = definition.dependencies
            db_tool.author = definition.author
            db_tool.documentation_url = definition.documentation_url

            await self.db.commit()
            return True

        except Exception as e:
            await self.db.rollback()
            print(f"Error updating tool: {e}")
            return False

    async def delete_tool(self, tool_id: str) -> bool:
        """Delete a tool definition"""
        try:
            query = select(ToolDefinitionDB).where(ToolDefinitionDB.id == tool_id)
            result = await self.db.execute(query)
            db_tool = result.scalar_one_or_none()

            if not db_tool:
                return False

            await self.db.delete(db_tool)
            await self.db.commit()
            return True

        except Exception as e:
            await self.db.rollback()
            print(f"Error deleting tool: {e}")
            return False

    def _validate_tool_definition(self, definition: ToolDefinition) -> bool:
        """Validate tool definition before registration"""
        # Add validation logic
        if not definition.name or not definition.description:
            return False

        # Validate schemas
        if not definition.input_schema or not definition.output_schema:
            return False

        return True

    def _db_to_pydantic(self, db_tool: ToolDefinitionDB) -> ToolDefinition:
        """
        Convert DB model to Pydantic model
        FIXED: Properly handle timezone-aware datetimes
        """
        from app.schemas.tool_definition import ToolCapability, CredentialRequirement
        from app.schemas.common import ToolCategory, ToolType

        # FIXED: Ensure datetimes are timezone-aware
        created_at = self._ensure_timezone_aware(db_tool.created_at)
        updated_at = self._ensure_timezone_aware(db_tool.updated_at)

        return ToolDefinition(
            id=db_tool.id,
            name=db_tool.name,
            description=db_tool.description,
            version=db_tool.version,
            icon=db_tool.icon,
            category=ToolCategory(db_tool.category),
            type=ToolType(db_tool.type),
            capabilities=[ToolCapability(**cap) for cap in db_tool.capabilities],
            tags=db_tool.tags,
            input_schema=db_tool.input_schema,
            output_schema=db_tool.output_schema,
            configuration_schema=db_tool.configuration_schema,
            credential_requirements=[
                CredentialRequirement(**req) for req in db_tool.credential_requirements
            ],
            dependencies=db_tool.dependencies,
            author=db_tool.author,
            documentation_url=db_tool.documentation_url,
            created_at=created_at,
            updated_at=updated_at,
        )

    def _ensure_timezone_aware(self, dt: datetime) -> datetime:
        """
        Ensure datetime is timezone-aware
        FIXED: Handle both timezone-aware and timezone-naive datetimes from database
        """
        if dt is None:
            return datetime.now(timezone.utc)

        if dt.tzinfo is None:
            # Timezone-naive datetime from database - assume UTC
            return dt.replace(tzinfo=timezone.utc)
        else:
            # Already timezone-aware
            return dt
