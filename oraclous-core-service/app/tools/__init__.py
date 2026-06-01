from app.tools.registry import tool_registry
from app.tools.factory import ToolFactory

# Importing each module triggers plugin_registry.register() + tool_registry.register_tool()
# at module scope — no hard-coded registration list required here.
# Try/except preserves graceful degradation when optional heavy dependencies
# (googleapiclient, aiomysql, asyncpg, notion-client, etc.) are absent.
try:
    from app.tools.implementations.ingestion.google_drive_reader import GoogleDriveReader  # noqa: F401
except ImportError:
    pass

try:
    from app.tools.implementations.ingestion.mysql_reader import MySQLReader  # noqa: F401
except ImportError:
    pass

try:
    from app.tools.implementations.ingestion.notion_reader import NotionReader  # noqa: F401
except ImportError:
    pass

try:
    from app.tools.implementations.ingestion.postgresql_reader import PostgreSQLReader  # noqa: F401
except ImportError:
    pass

__all__ = ["tool_registry", "ToolFactory"]
