"""Built-in tool plugins (ORAA-4 §21 domain layer).

The registry's seeded catalogue — the connector tool *descriptors* (manifests). Their executors land
in S4/S5; here they are real registry entries (id, version, metadata, spec, credential
requirements). Each registers itself against ``plugin_registry`` at import; the startup hook
(``services/plugin_sync``) seeds them idempotently. Reshaped from the legacy
``oraclous-core-service/app/tools/implementations/ingestion/*`` tool definitions.
"""

from __future__ import annotations

from oraclous_capability_registry_service.domain.plugins.base import (
    CapabilityKindPlugin,
    plugin_registry,
)
from oraclous_capability_registry_service.domain.tool_id import generate_tool_id
from oraclous_capability_registry_service.models.enums import DescriptorKind

_VERSION = "1.0.0"
_CATEGORY = "INGESTION"


class _ConnectorToolPlugin(CapabilityKindPlugin):
    """DRY base: a connector tool described by class attributes. Subclasses set the attrs."""

    NAME: str
    DESCRIPTION: str
    TYPE: str  # INTERNAL | API | MCP
    TAGS: list[str]
    CAPABILITIES: list[dict]
    CREDENTIAL_REQUIREMENTS: list[dict]
    INPUT_SCHEMA: dict
    OUTPUT_SCHEMA: dict
    CONFIGURATION_SCHEMA: dict | None = None
    CATEGORY: str = _CATEGORY  # the connector readers are INGESTION; the retriever overrides it

    @classmethod
    def plugin_id(cls) -> str:
        return str(generate_tool_id(cls.NAME, _VERSION, cls.CATEGORY))

    @classmethod
    def kind(cls) -> DescriptorKind:
        return DescriptorKind.TOOL

    @classmethod
    def descriptor(cls) -> dict:
        return {
            "kind": "tool",
            "id": cls.plugin_id(),
            "version": {"semver": _VERSION, "tags": list(cls.TAGS)},
            "metadata": {
                "name": cls.NAME,
                "description": cls.DESCRIPTION,
                "category": cls.CATEGORY,
                "icon": None,
                "documentation_url": None,
            },
            "spec": {
                "type": cls.TYPE,
                "capabilities": list(cls.CAPABILITIES),
                "input_schema": cls.INPUT_SCHEMA,
                "output_schema": cls.OUTPUT_SCHEMA,
                "configuration_schema": cls.CONFIGURATION_SCHEMA,
                "credential_requirements": list(cls.CREDENTIAL_REQUIREMENTS),
                "dependencies": [],
            },
        }


_ROWS_OUTPUT = {
    "type": "object",
    "properties": {"rows": {"type": "array", "items": {"type": "object"}}},
}
_DOCS_OUTPUT = {
    "type": "object",
    "properties": {"documents": {"type": "array", "items": {"type": "object"}}},
}


@plugin_registry.register
class PostgreSQLReaderPlugin(_ConnectorToolPlugin):
    NAME = "PostgreSQL Reader"
    DESCRIPTION = "Read rows and list tables from a PostgreSQL database via a connection string."
    TYPE = "INTERNAL"
    TAGS = ["postgresql", "relational", "database"]
    CAPABILITIES = [
        {"name": "list_tables", "description": "List the tables in the database", "parameters": {}},
        {
            "name": "query",
            "description": "Run a parameterized read-only query",
            "parameters": {"query": "string", "params": "object"},
        },
    ]
    CREDENTIAL_REQUIREMENTS = [
        {"type": "connection_string", "provider": "postgresql", "required": True}
    ]
    INPUT_SCHEMA = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "params": {"type": "object"}},
    }
    OUTPUT_SCHEMA = _ROWS_OUTPUT


@plugin_registry.register
class MySQLReaderPlugin(_ConnectorToolPlugin):
    NAME = "MySQL Reader"
    DESCRIPTION = "Read rows and list tables from a MySQL database via a connection string."
    TYPE = "INTERNAL"
    TAGS = ["mysql", "relational", "database"]
    CAPABILITIES = [
        {"name": "list_tables", "description": "List the tables in the database", "parameters": {}},
        {
            "name": "query",
            "description": "Run a parameterized read-only query",
            "parameters": {"query": "string", "params": "object"},
        },
    ]
    CREDENTIAL_REQUIREMENTS = [{"type": "connection_string", "provider": "mysql", "required": True}]
    INPUT_SCHEMA = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "params": {"type": "object"}},
    }
    OUTPUT_SCHEMA = _ROWS_OUTPUT


@plugin_registry.register
class NotionReaderPlugin(_ConnectorToolPlugin):
    NAME = "Notion Reader"
    DESCRIPTION = "Read pages and databases from a Notion workspace via an integration token."
    TYPE = "API"
    TAGS = ["notion", "saas", "documents"]
    CAPABILITIES = [
        {
            "name": "read_page",
            "description": "Read a Notion page",
            "parameters": {"page_id": "str"},
        },
        {"name": "search", "description": "Search the workspace", "parameters": {"query": "str"}},
    ]
    CREDENTIAL_REQUIREMENTS = [{"type": "api_key", "provider": "notion", "required": True}]
    INPUT_SCHEMA = {"type": "object", "properties": {"page_id": {"type": "string"}}}
    OUTPUT_SCHEMA = _DOCS_OUTPUT


@plugin_registry.register
class GitHubReaderPlugin(_ConnectorToolPlugin):
    NAME = "GitHub Reader"
    DESCRIPTION = "Read repository files and metadata from GitHub via a personal access token."
    TYPE = "API"
    TAGS = ["github", "saas", "code"]
    CAPABILITIES = [
        {
            "name": "list_files",
            "description": "List files in a repository path",
            "parameters": {"repo": "str", "path": "str"},
        },
        {
            "name": "read_file",
            "description": "Read a file's contents",
            "parameters": {"repo": "str", "path": "str"},
        },
    ]
    CREDENTIAL_REQUIREMENTS = [{"type": "api_key", "provider": "github", "required": True}]
    INPUT_SCHEMA = {
        "type": "object",
        "properties": {"repo": {"type": "string"}, "path": {"type": "string"}},
    }
    OUTPUT_SCHEMA = _DOCS_OUTPUT


@plugin_registry.register
class GoogleDriveReaderPlugin(_ConnectorToolPlugin):
    NAME = "Google Drive Reader"
    DESCRIPTION = "Read files from Google Drive via an OAuth token (drive.readonly scope)."
    TYPE = "API"
    TAGS = ["google", "drive", "saas", "documents"]
    CAPABILITIES = [
        {"name": "list_files", "description": "List Drive files", "parameters": {"query": "str"}},
        {
            "name": "read_file",
            "description": "Read a Drive file's contents",
            "parameters": {"file_id": "str"},
        },
    ]
    CREDENTIAL_REQUIREMENTS = [
        {
            "type": "oauth_token",
            "provider": "google",
            "required": True,
            "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
        }
    ]
    INPUT_SCHEMA = {"type": "object", "properties": {"file_id": {"type": "string"}}}
    OUTPUT_SCHEMA = _DOCS_OUTPUT


_HITS_OUTPUT = {
    "type": "object",
    "properties": {
        "hits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "type": {"type": "string"},
                    "properties": {"type": "object"},
                },
            },
        }
    },
}


@plugin_registry.register
class KnowledgeRetrieverPlugin(_ConnectorToolPlugin):
    """First-party retrieval over the org's knowledge graph — the in-loop tool a Wave-1 "QA over
    your graph" agent binds as ``core/knowledge-retriever@1.0.0``. No credential: it is reached over
    the internal/gateway-trust path, the caller's org identity forwarded by the executor (never a
    BYOM key). The ``search`` operation wraps the retriever's ``/v1/search/{mode}``."""

    NAME = "Knowledge Retriever"  # slug ``knowledge-retriever`` MUST match the ref's name slug
    CATEGORY = "RETRIEVAL"
    DESCRIPTION = (
        "Search the organisation's knowledge graph (semantic, fulltext, or hybrid) and return the "
        "matching nodes. First-party and org-scoped; no credential required."
    )
    TYPE = "INTERNAL"
    TAGS = ["knowledge-graph", "retrieval", "search", "rag"]
    CAPABILITIES = [
        {
            "name": "search",
            "description": "Search a knowledge graph and return the matching nodes (hits).",
            "parameters": {
                "graph_id": "str",
                "query": "str",
                "top_k": "int",
                "mode": "str",  # semantic (default) | fulltext | hybrid
            },
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # first-party: reached over the internal trust path
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["graph_id", "query"],
        "properties": {
            "graph_id": {"type": "string", "format": "uuid"},
            "query": {"type": "string", "minLength": 1},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            "mode": {"type": "string", "enum": ["semantic", "fulltext", "hybrid"]},
        },
    }
    OUTPUT_SCHEMA = _HITS_OUTPUT
