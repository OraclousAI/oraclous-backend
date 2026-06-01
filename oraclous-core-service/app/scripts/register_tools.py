"""
Script to register all available tools with the in-memory executor registry.
"""

import asyncio
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tools.registry import tool_registry

from app.tools.implementations.ingestion.google_drive_reader import GoogleDriveReader
from app.tools.implementations.ingestion.notion_reader import NotionReader
from app.tools.implementations.ingestion.postgresql_reader import PostgreSQLReader
from app.tools.implementations.ingestion.mysql_reader import MySQLReader


def register_tools():
    """Register tools with the in-memory executor registry."""
    tool_registry.register_tool(GoogleDriveReader, GoogleDriveReader.get_tool_definition())
    tool_registry.register_tool(NotionReader, NotionReader.get_tool_definition())
    tool_registry.register_tool(PostgreSQLReader, PostgreSQLReader.get_tool_definition())
    tool_registry.register_tool(MySQLReader, MySQLReader.get_tool_definition())
    print(f"Registered {len(tool_registry.list_definitions())} tools in memory")


if __name__ == "__main__":
    register_tools()
