"""Built-in tool plugins (domain layer).

The registry's seeded catalogue — the connector tool *descriptors* (manifests). Their executors land
in S4/S5; here they are real registry entries (id, version, metadata, spec, credential
requirements). Each registers itself against ``plugin_registry`` at import; the startup hook
(``services/plugin_sync``) seeds them idempotently. Reshaped from the legacy
``oraclous-core-service/app/tools/implementations/ingestion/*`` tool definitions.
"""

from __future__ import annotations

from oraclous_capability_registry_service.domain.connectors.source_providers import (
    available_sources,
)
from oraclous_capability_registry_service.domain.libraries import registry as library_registry
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


_JOB_OUTPUT = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "status": {"type": "string"},
    },
}


@plugin_registry.register
class GraphIngestPlugin(_ConnectorToolPlugin):
    """First-party ingestion INTO the org's knowledge graph — the write twin of the retriever. An
    agent's OHM binds it as ``core/graph-ingest@1.0.0`` to enqueue ingestion of content into a graph
    it owns. No credential: reached over the internal/gateway-trust path, the caller's org identity
    forwarded by the executor (never a BYOM key). The ``ingest`` operation wraps the KGS's
    ``/internal/v1/ingest`` and returns the enqueued job."""

    NAME = "Graph Ingest"  # slug ``graph-ingest`` MUST match the ref's name slug
    CATEGORY = "INGESTION"
    DESCRIPTION = (
        "Ingest content into the organisation's knowledge graph and return the enqueued job. "
        "First-party and org-scoped; no credential required."
    )
    TYPE = "INTERNAL"
    TAGS = ["knowledge-graph", "ingestion", "ingest"]
    CAPABILITIES = [
        {
            "name": "ingest",
            "description": "Enqueue ingestion of content into a knowledge graph (returns a job).",
            "parameters": {
                "graph_id": "str",
                "content": "str",
                "source_type": "str",  # text (default) | md | csv | json | ...
                "recipe_id": "str",  # structured only: a stored recipe id (optional)
            },
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # first-party: reached over the internal trust path
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["content"],
        "properties": {
            # graph substrate (#524): the run binds a graph, so graph_id is OPTIONAL — omit it and
            # the bound graph is used (never invent a UUID). Only set it to target a different graph
            # your org owns. KGS RLS scopes it to the caller's org either way.
            "graph_id": {
                "type": "string",
                "format": "uuid",
                "description": "Optional; defaults to the run's bound graph. Omit unless targeting "
                "a different graph your org owns.",
            },
            "content": {"type": "string", "minLength": 1},
            "source_type": {"type": "string"},
            "recipe_id": {"type": "string"},
        },
    }
    OUTPUT_SCHEMA = _JOB_OUTPUT


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
        "required": ["query"],
        "properties": {
            # graph substrate (#524): the run binds a graph, so graph_id is OPTIONAL — omit it and
            # the bound graph is searched (never invent a UUID). Only set it to target a different
            # graph your org owns. KGS RLS scopes it to the caller's org either way.
            "graph_id": {
                "type": "string",
                "format": "uuid",
                "description": "Optional; defaults to the run's bound graph. Omit unless targeting "
                "a different graph your org owns.",
            },
            "query": {"type": "string", "minLength": 1},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            "mode": {"type": "string", "enum": ["semantic", "fulltext", "hybrid"]},
        },
    }
    OUTPUT_SCHEMA = _HITS_OUTPUT


_MEMORIES_OUTPUT = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string"},
                    "type": {"type": "string"},
                    "content": {"type": "string"},
                    "importance_score": {"type": "number"},
                    "relevance_score": {"type": "number"},
                    "confidence": {"type": "number"},
                    "scope": {"type": "string"},
                },
            },
        },
        "total": {"type": "integer"},
    },
}


@plugin_registry.register
class RecallMemoryPlugin(_ConnectorToolPlugin):
    """First-party agent-memory recall (#332 / ADR-027 §6) — the tool an agent OPTS INTO via its
    OHM toolset as ``core/recall-memory@1.0.0`` to remember past runs, facts and preferences. No
    change to the harness's default prompt assembly, so existing runs carry zero risk. No
    credential: reached over the internal/gateway-trust path, the caller's org identity forwarded
    by the executor (never a BYOM key). The ``recall_memory`` operation wraps the KGS's
    ``/api/v1/graphs/{graph_id}/memories/search`` (hybrid fulltext + vector + Ebbinghaus
    importance + recency)."""

    NAME = "Recall Memory"  # slug ``recall-memory`` MUST match the ref's name slug
    CATEGORY = "RETRIEVAL"
    DESCRIPTION = (
        "Recall the organisation's agent memories (episodic runs, semantic facts, procedural "
        "preferences) ranked by relevance, importance and recency. First-party and org-scoped; "
        "no credential required."
    )
    TYPE = "INTERNAL"
    TAGS = ["knowledge-graph", "memory", "recall", "retrieval"]
    CAPABILITIES = [
        {
            "name": "recall_memory",
            "description": "Search the agent memory store and return the matching memories.",
            "parameters": {
                "graph_id": "str",
                "query": "str",
                "type": "str",  # episodic | semantic | procedural (optional)
                "scope": "str",  # session | user | agent | team | organization (optional)
                "limit": "int",
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
            "type": {"type": "string", "enum": ["episodic", "semantic", "procedural"]},
            "scope": {
                "type": "string",
                "enum": ["session", "user", "agent", "team", "organization"],
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
        },
    }
    OUTPUT_SCHEMA = _MEMORIES_OUTPUT


@plugin_registry.register
class FindSimilarPlugin(_ConnectorToolPlugin):
    """First-party "entities similar to X" over the org's knowledge graph (#310) — the read twin of
    the knowledge-retriever that, given a node, returns the ``SIMILAR_TO`` neighbours the KGS
    similarity pass wrote, ranked by the stamped cosine. No credential: reached over the
    internal/gateway-trust path, the caller's org identity forwarded by the executor (never a BYOM
    key). The ``find_similar`` operation wraps the retriever's
    ``/v1/graph/{graph_id}/similar/{node_id}``."""

    NAME = "Find Similar"  # slug ``find-similar`` MUST match the ref's name slug
    CATEGORY = "RETRIEVAL"
    DESCRIPTION = (
        "Find the entities most similar to a given node in the organisation's knowledge graph "
        "(the SIMILAR_TO neighbours, ranked by similarity). First-party and org-scoped; no "
        "credential required."
    )
    TYPE = "INTERNAL"
    TAGS = ["knowledge-graph", "retrieval", "similarity", "similar"]
    CAPABILITIES = [
        {
            "name": "find_similar",
            "description": "Return the nodes most similar to a given node (SIMILAR_TO neighbours).",
            "parameters": {
                "graph_id": "str",
                "node_id": "str",
                "top_k": "int",
                "min_score": "float",  # 0.0 returns every SIMILAR_TO link; raise to keep close ones
            },
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # first-party: reached over the internal trust path
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["node_id"],
        "properties": {
            # graph substrate (#524): the run binds a graph, so graph_id is OPTIONAL — omit it and
            # the bound graph is used (never invent a UUID). Only set it to target a different graph
            # your org owns. KGS RLS scopes it to the caller's org either way.
            "graph_id": {
                "type": "string",
                "format": "uuid",
                "description": "Optional; defaults to the run's bound graph. Omit unless targeting "
                "a different graph your org owns.",
            },
            "node_id": {"type": "string", "minLength": 1},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            "min_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.0},
        },
    }
    OUTPUT_SCHEMA = _HITS_OUTPUT


@plugin_registry.register
class FederatedSearchPlugin(_ConnectorToolPlugin):
    """First-party federated cross-graph search (#330 / ADR-026) — search ALL the workspaces the
    caller can read from one place, bound as ``core/federated-search@1.0.0``. No credential:
    reached over the internal/gateway-trust path with the caller's org identity forwarded by the
    executor; the retriever enumerates the accessible set itself, so federation grants NO new
    access in-loop. The ``federated_search`` operation wraps the retriever's
    ``POST /v1/federated/search``; every hit is labeled ``source_graph_id``/``source_graph_name``.
    """

    NAME = "Federated Search"  # slug ``federated-search`` MUST match the ref's name slug
    CATEGORY = "RETRIEVAL"
    DESCRIPTION = (
        "Search ALL the knowledge graphs the caller can access from one place (entity, semantic, "
        "fulltext, or hybrid) — every result labeled with its source graph. First-party and "
        "org-scoped; federation grants no new access; no credential required."
    )
    TYPE = "INTERNAL"
    TAGS = ["knowledge-graph", "retrieval", "search", "federation", "cross-graph"]
    CAPABILITIES = [
        {
            "name": "federated_search",
            "description": (
                "Search across all accessible graphs (or an explicit accessible subset) and "
                "return labeled hits."
            ),
            "parameters": {
                "query": "str",
                "mode": "str",  # hybrid (default) | entity | semantic | fulltext
                "graph_ids": "list[str]",  # optional subset; fail-closed if any is inaccessible
                "per_graph_k": "int",
                "total_k": "int",
            },
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # first-party: reached over the internal trust path
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "mode": {
                "type": "string",
                "enum": ["entity", "semantic", "fulltext", "hybrid"],
                "default": "hybrid",
            },
            "graph_ids": {
                "type": "array",
                "items": {"type": "string", "format": "uuid"},
            },
            "per_graph_k": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
            "total_k": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
    }
    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "results": {"type": "array", "items": {"type": "object"}},
            "meta": {"type": "object"},
        },
    }


@plugin_registry.register
class WebResearchPlugin(_ConnectorToolPlugin):
    """Pre-registered live-web research tool group (#486 / ADR-039 D1) — bound as
    ``core/web-research@1.0.0``. Three operations: ``search`` the live web (BYOM ``api_key`` via the
    SearchProvider factory — Tavily first), ``fetch`` a URL's raw text, ``read`` a URL as readable
    text. The tool is **key-gated** by a per-org web-search credential — ``search`` consumes it;
    ``fetch``/``read`` do not, but the group carries one key. The gap that left EURail's researchers
    reason-only (north-star item 5)."""

    NAME = "Web Research"  # slug ``web-research`` MUST match the ref's name slug
    CATEGORY = "RESEARCH"
    DESCRIPTION = (
        "Live-web research: search the web (bring-your-own search api_key), fetch a URL's raw "
        "text, or read a URL as readable text. Internal/private targets are refused (SSRF-safe)."
    )
    TYPE = "API"
    TAGS = ["web", "search", "research", "live-web"]
    CAPABILITIES = [
        {
            "name": "search",
            "description": "Search the live web and return ranked hits (BYOM api_key).",
            "parameters": {"query": "str", "max_results": "int", "provider": "str"},
        },
        {
            "name": "fetch",
            "description": "HTTP GET a URL and return its raw text body.",
            "parameters": {"url": "str"},
        },
        {
            "name": "read",
            "description": "HTTP GET a URL and return readable text (tags/script stripped).",
            "parameters": {"url": "str"},
        },
    ]
    # A per-org web-search api_key, resolved at dispatch (ADR-038 D3 / ADR-008). REQUIRED so the
    # dispatch path resolves it — the registry resolves only `required` credentials, with no per-op
    # concept, so a mixed tool group is key-gated as a whole: `search` consumes the key; `fetch`/
    # `read` don't, but the instance must carry one. An unconfigured instance fails closed (409).
    CREDENTIAL_REQUIREMENTS = [{"type": "api_key", "provider": "web_search", "required": True}]
    INPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["search", "fetch", "read"]},
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
            "provider": {"type": "string"},
            "url": {"type": "string"},
        },
    }
    OUTPUT_SCHEMA = {"type": "object"}


@plugin_registry.register
class ScriptIngestionPlugin(_ConnectorToolPlugin):
    """Adopt a curated loader as a scheduled-ingestion tool (#487 / ADR-038 D1) — bound as
    ``core/script-ingestion@1.0.0``. The ``run`` op runs a curated loader (by ``loader_id``,
    never a free argv) as a guarded subprocess and lands its JSON output on the org-scoped Execution
    row. The cron that fires it on a cadence is #489; this is the manual-dispatch executor it will
    schedule. User-supplied loader adoption (HITL) is a follow-up; only curated loaders here."""

    NAME = "Script Ingestion"  # slug ``script-ingestion`` MUST match the ref's name slug
    CATEGORY = "INGESTION"
    DESCRIPTION = (
        "Run a curated ingestion loader as a guarded subprocess and capture its JSON output to the "
        "org store. Curated loaders only (no arbitrary commands); resource + time capped."
    )
    TYPE = "INTERNAL"
    TAGS = ["ingestion", "loader", "script", "scheduled"]
    CAPABILITIES = [
        {
            "name": "run",
            "description": "Run a curated loader by id and capture its JSON output.",
            "parameters": {"loader_id": "str", "args": "object", "graph_id": "str"},
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # the curated synthetic loaders are keyless
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["loader_id"],
        "properties": {
            "loader_id": {"type": "string", "minLength": 1},
            "args": {"type": "object"},
            "graph_id": {"type": "string", "format": "uuid"},
            "source_type": {"type": "string"},
        },
    }
    OUTPUT_SCHEMA = {"type": "object"}


@plugin_registry.register
class LibraryGroupPlugin(_ConnectorToolPlugin):
    """Mount a curated in-repo library as a typed tool group (#488 / ADR-038 D1) — bound as
    ``core/text-tools@1.0.0``. One operation per exported function (CAPABILITIES + the op enum
    are GENERATED from ``domain/libraries/registry`` so they never drift from the callables). Each
    operation is dispatched in-process by :class:`LibraryGroupExecutor`. Curated, trusted, keyless;
    user-supplied library adoption (subprocess + HITL) is a follow-up."""

    NAME = "Text Tools"  # slug ``text-tools`` MUST match the ref's name slug
    CATEGORY = "TRANSFORM"
    DESCRIPTION = (
        "A curated in-repo library exposed as a typed tool group: one operation per exported "
        "function (word_count / to_upper / extract_emails). Deterministic, keyless, in-process."
    )
    TYPE = "INTERNAL"
    TAGS = ["library", "transform", "text", "curated"]
    CAPABILITIES = library_registry.capabilities()  # one per function, generated from the registry
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # curated, in-process, keyless
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {"type": "string", "enum": library_registry.operation_names()},
            "text": {"type": "string"},
        },
    }
    OUTPUT_SCHEMA = {"type": "object"}


@plugin_registry.register
class RestConnectorPlugin(_ConnectorToolPlugin):
    """Curated REST data-source connector (#489 / ADR-039 D1), bound ``core/rest-connector@1.0.0``.
    The ``fetch`` op reads a curated source/endpoint (``source_id`` selects a provider from
    ``domain/connectors/source_providers``, never a free URL) over a SSRF-guarded HTTPS GET and
    returns its parsed dict. The two shipped sources (mempool.space, alternative.me) are keyless
    public GETs; keyed sources (FRED/CoinMetrics/Binance) slot in as BYOM providers, a follow-up."""

    NAME = "REST Connector"  # slug ``rest-connector`` MUST match the ref's name slug
    CATEGORY = "CONNECTOR"
    DESCRIPTION = (
        "Fetch a curated external data source over a SSRF-guarded HTTPS GET. Sources are curated "
        "(no free URL); the shipped set is keyless public data (mempool.space, alternative.me)."
    )
    TYPE = "API"
    TAGS = ["connector", "rest", "data", "bitcoin"]
    CAPABILITIES = [
        {
            "name": "fetch",
            "description": "Fetch a curated source endpoint and return its parsed data.",
            "parameters": {"source_id": "str", "endpoint": "str"},
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # the shipped sources are keyless
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["source_id", "endpoint"],
        "properties": {
            "source_id": {"type": "string", "enum": available_sources()},
            "endpoint": {"type": "string", "minLength": 1},
        },
    }
    OUTPUT_SCHEMA = {"type": "object"}


_TEXT_OUTPUT = {"type": "object"}


@plugin_registry.register
class ReadToolPlugin(_ConnectorToolPlugin):
    """Standard agent toolset (#440 / #507) — ``Read``, bound ``core/read@1``. Reads a UTF-8 text
    file from the per-org sandbox workspace. ``NAME`` slugifies to exactly ``read`` so the
    importer's ``core/read@1`` ref resolves. Keyless; sandbox-confined; no host access."""

    NAME = "Read"  # slug ``read`` MUST match the imported ref's name slug
    CATEGORY = "FILESYSTEM"
    DESCRIPTION = "Read a UTF-8 text file from the agent's sandbox workspace."
    TYPE = "INTERNAL"
    TAGS = ["standard", "filesystem", "read", "agent-tool"]
    CAPABILITIES = [
        {
            "name": "read",
            "description": "Read a text file from the sandbox and return its content.",
            "parameters": {"path": "str"},
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # sandbox-confined; keyless
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["path"],
        "properties": {"path": {"type": "string", "minLength": 1}},
    }
    OUTPUT_SCHEMA = _TEXT_OUTPUT


@plugin_registry.register
class WriteToolPlugin(_ConnectorToolPlugin):
    """Standard agent toolset (#440 / #507) — ``Write``, bound ``core/write@1``. Writes text to a
    path in the per-org sandbox workspace (parent dirs created). Keyless; sandbox-confined."""

    NAME = "Write"  # slug ``write`` MUST match the imported ref's name slug
    CATEGORY = "FILESYSTEM"
    DESCRIPTION = "Write text to a file in the agent's sandbox workspace."
    TYPE = "INTERNAL"
    TAGS = ["standard", "filesystem", "write", "agent-tool"]
    CAPABILITIES = [
        {
            "name": "write",
            "description": "Write text to a sandbox file and return the byte count.",
            "parameters": {"path": "str", "content": "str"},
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # sandbox-confined; keyless
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["path", "content"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "content": {"type": "string"},
        },
    }
    OUTPUT_SCHEMA = _TEXT_OUTPUT


@plugin_registry.register
class EditToolPlugin(_ConnectorToolPlugin):
    """Standard agent toolset (#440 / #507) — ``Edit``, bound ``core/edit@1``. String-replaces a
    unique ``old_string`` with ``new_string`` in a sandbox file. Keyless; sandbox-confined."""

    NAME = "Edit"  # slug ``edit`` MUST match the imported ref's name slug
    CATEGORY = "FILESYSTEM"
    DESCRIPTION = "Replace a unique substring in a file in the agent's sandbox workspace."
    TYPE = "INTERNAL"
    TAGS = ["standard", "filesystem", "edit", "agent-tool"]
    CAPABILITIES = [
        {
            "name": "edit",
            "description": "Replace old_string with new_string in a sandbox file (must be unique).",
            "parameters": {"path": "str", "old_string": "str", "new_string": "str"},
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # sandbox-confined; keyless
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["path", "old_string", "new_string"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "old_string": {"type": "string", "minLength": 1},
            "new_string": {"type": "string"},
        },
    }
    OUTPUT_SCHEMA = _TEXT_OUTPUT


@plugin_registry.register
class GrepToolPlugin(_ConnectorToolPlugin):
    """Standard agent toolset (#440 / #507) — ``Grep``, bound ``core/grep@1``. Regex-searches the
    per-org sandbox (bounded) and returns the matching lines. Keyless; sandbox-confined."""

    NAME = "Grep"  # slug ``grep`` MUST match the imported ref's name slug
    CATEGORY = "FILESYSTEM"
    DESCRIPTION = "Regex-search files in the agent's sandbox workspace and return matching lines."
    TYPE = "INTERNAL"
    TAGS = ["standard", "filesystem", "grep", "search", "agent-tool"]
    CAPABILITIES = [
        {
            "name": "grep",
            "description": "Search the sandbox for a regex and return matching lines (bounded).",
            "parameters": {"pattern": "str", "path": "str"},
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # sandbox-confined; keyless
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["pattern"],
        "properties": {
            "pattern": {"type": "string", "minLength": 1},
            "path": {"type": "string"},
        },
    }
    OUTPUT_SCHEMA = _TEXT_OUTPUT


@plugin_registry.register
class GlobToolPlugin(_ConnectorToolPlugin):
    """Standard agent toolset (#440 / #507) — ``Glob``, bound ``core/glob@1``. Lists per-org sandbox
    paths matching a glob pattern. Keyless; sandbox-confined."""

    NAME = "Glob"  # slug ``glob`` MUST match the imported ref's name slug
    CATEGORY = "FILESYSTEM"
    DESCRIPTION = "List files in the agent's sandbox workspace matching a glob pattern."
    TYPE = "INTERNAL"
    TAGS = ["standard", "filesystem", "glob", "agent-tool"]
    CAPABILITIES = [
        {
            "name": "glob",
            "description": "List sandbox paths matching a glob pattern (bounded).",
            "parameters": {"pattern": "str"},
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # sandbox-confined; keyless
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["pattern"],
        "properties": {"pattern": {"type": "string", "minLength": 1}},
    }
    OUTPUT_SCHEMA = _TEXT_OUTPUT


@plugin_registry.register
class BashToolPlugin(_ConnectorToolPlugin):
    """Standard agent toolset (#440 / #507) — ``Bash``, bound ``core/bash@1``. Runs a command in the
    per-org sandbox as a guarded subprocess (RLIMIT + process-group kill + capped output + clean
    minimal env; the cwd is the sandbox root). Keyless; output-capped; secrets never echoed."""

    NAME = "Bash"  # slug ``bash`` MUST match the imported ref's name slug
    CATEGORY = "EXECUTION"
    DESCRIPTION = (
        "Run a shell command in the agent's sandbox workspace as a guarded subprocess "
        "(resource-capped, time-limited, output-capped; the registry's secrets are never exposed)."
    )
    TYPE = "INTERNAL"
    TAGS = ["standard", "execution", "bash", "shell", "agent-tool"]
    CAPABILITIES = [
        {
            "name": "bash",
            "description": "Run a shell command in the sandbox and return stdout/stderr/exit_code.",
            "parameters": {"command": "str"},
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # sandbox-confined; keyless
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["command"],
        "properties": {"command": {"type": "string", "minLength": 1}},
    }
    OUTPUT_SCHEMA = _TEXT_OUTPUT


@plugin_registry.register
class WebSearchToolPlugin(_ConnectorToolPlugin):
    """Standard agent toolset (#440 / #507) — ``WebSearch``, bound ``core/websearch@1``. Searches
    the live web; delegates to the web-research search path (BYOM ``api_key`` via the provider
    factory). ``NAME`` is the single word ``WebSearch`` so it slugifies to exactly ``websearch``
    (the importer's ref), NOT ``web-search``. Key-gated like Web Research's ``search``."""

    NAME = "WebSearch"  # slug ``websearch`` (one word — NOT ``web-search``) MUST match the ref slug
    CATEGORY = "RESEARCH"
    DESCRIPTION = "Search the live web and return ranked hits (bring-your-own search api_key)."
    TYPE = "API"
    TAGS = ["standard", "web", "search", "agent-tool"]
    CAPABILITIES = [
        {
            "name": "search",
            "description": "Search the live web and return ranked hits.",
            "parameters": {"query": "str", "max_results": "int"},
        },
    ]
    # A per-org web-search api_key (same as Web Research's `search`); resolved at dispatch. REQUIRED
    # so the dispatch path resolves it; an unconfigured instance fails closed.
    CREDENTIAL_REQUIREMENTS = [{"type": "api_key", "provider": "web_search", "required": True}]
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
        },
    }
    OUTPUT_SCHEMA = _TEXT_OUTPUT


@plugin_registry.register
class WebFetchToolPlugin(_ConnectorToolPlugin):
    """Standard agent toolset (#440 / #507) — ``WebFetch``, bound ``core/webfetch@1``. HTTP-GETs a
    URL and returns its text; delegates to the web-research fetch path (the shared SSRF-guarded
    egress gate + per-hop redirect re-validation). ``NAME`` is the single word ``WebFetch`` so it
    slugifies to exactly ``webfetch`` (the importer's ref), NOT ``web-fetch``. Keyless."""

    NAME = "WebFetch"  # slug ``webfetch`` (one word — NOT ``web-fetch``) MUST match the ref slug
    CATEGORY = "RESEARCH"
    DESCRIPTION = (
        "HTTP GET a URL and return its text body. Internal/private targets are refused (SSRF-safe)."
    )
    TYPE = "API"
    TAGS = ["standard", "web", "fetch", "agent-tool"]
    CAPABILITIES = [
        {
            "name": "fetch",
            "description": "HTTP GET a URL and return its raw text body.",
            "parameters": {"url": "str"},
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # fetch is keyless (SSRF-guarded)
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["url"],
        "properties": {"url": {"type": "string", "minLength": 1}},
    }
    OUTPUT_SCHEMA = _TEXT_OUTPUT


@plugin_registry.register
class SendToDraftsPlugin(_ConnectorToolPlugin):
    """Delivery SINK (#489 / ADR-039 D1) — bound as ``core/send-to-drafts@1.0.0``. The ``send`` op
    records a delivery as a DRAFT (never published) on the org Execution row. The structural
    boundary: a generator can only deliver through this declared, ceiling-gated sink, and the sink
    cannot send — an external publish is a separate human-gated step. Keyless, content-capped."""

    NAME = "Send to Drafts"  # slug ``send-to-drafts`` MUST match the ref's name slug
    CATEGORY = "DELIVERY"
    DESCRIPTION = (
        "Record a delivery (channel + content) as a DRAFT on the org drafts queue. This sink only "
        "drafts — it never sends or publishes; an external send is a separate human-gated step."
    )
    TYPE = "INTERNAL"
    TAGS = ["delivery", "sink", "drafts", "notification"]
    CAPABILITIES = [
        {
            "name": "send",
            "description": "Record a delivery as a DRAFT (never published).",
            "parameters": {"channel": "str", "content": "str", "recipient": "str"},
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = []  # keyless; drafts only, no external credential
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["channel", "content"],
        "properties": {
            "channel": {"type": "string", "enum": ["email", "slack", "notification", "webhook"]},
            "content": {"type": "string", "minLength": 1},
            "recipient": {"type": "string"},
        },
    }
    OUTPUT_SCHEMA = {"type": "object"}


@plugin_registry.register
class GitHubSinkPlugin(_ConnectorToolPlugin):
    """Deliver-back git-tree SINK (#515 / E6 O7) — bound as ``core/github-sink@1.0.0``. The
    ``deliver`` op writes a team's outputs into the user's git tree (a head branch + a PR) via the
    GitHub/Gitea-common Contents API, with Oraclous owning the clean-delta (an identical re-deliver
    is a NO_OP; a changed file writes only that diff). A DISTINCT capability from the read-only
    ``GitHub Reader`` — write is never folded into the read tool. PAT via the broker, egress-gated.
    """

    NAME = "GitHub Sink"  # slug ``github-sink`` MUST match the ref's name slug
    CATEGORY = "DELIVERY"
    DESCRIPTION = (
        "Deliver files into a user's git tree (github or gitea) on a head branch + a PR via the "
        "Contents API. A recurring deliver writes a clean diff, never a clobber (identical→NO_OP)."
    )
    TYPE = "API"
    TAGS = ["delivery", "sink", "github", "gitea", "git"]
    CAPABILITIES = [
        {
            "name": "deliver",
            "description": "Write changed files to a head branch + open a PR (clean-delta).",
            "parameters": {
                "repo": "str",
                "base_branch": "str",
                "head_branch": "str",
                "files": "list",
            },
        },
    ]
    CREDENTIAL_REQUIREMENTS: list[dict] = [
        {"type": "api_key", "provider": "github", "required": True}
    ]
    CONFIGURATION_SCHEMA = {
        "type": "object",
        "properties": {
            "forge": {"type": "string", "enum": ["github", "gitea"], "default": "github"},
            "base_url": {"type": "string"},
        },
    }
    INPUT_SCHEMA = {
        "type": "object",
        "required": ["operation", "repo", "files"],
        "properties": {
            "operation": {"type": "string", "enum": ["deliver"]},
            "repo": {"type": "string"},
            "base_branch": {"type": "string"},
            "head_branch": {"type": "string"},
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["path", "content"],
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            },
            "commit_message": {"type": "string"},
            "pr_title": {"type": "string"},
            "pr_body": {"type": "string"},
        },
    }
    OUTPUT_SCHEMA = {"type": "object"}
