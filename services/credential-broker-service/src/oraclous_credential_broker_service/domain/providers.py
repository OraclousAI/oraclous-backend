"""Provider/data-source capability catalogue (domain layer). Pure, no I/O.

Lifted from the legacy ``credential-broker-service/app/core/constants.py`` DATA_SOURCE_CAPABILITIES:
which data sources each provider exposes, the OAuth scopes each requires, and the operations it
supports. Drives runtime-token scope validation (S3) and data-source discovery (S4).
"""

from __future__ import annotations

from uuid import UUID

# Sentinel tool_id for provider-scoped OAuth-connect credentials. OAuth credentials are resolved by
# (org, user, provider) — never by tool_id — so a connected provider is stored as a single
# provider-scoped row carrying this sentinel instead of a real tool's id ("\x00oauth" in the tail).
OAUTH_CONNECT_TOOL_ID = UUID("00000000-0000-4000-8000-006f61757468")

DATA_SOURCE_CAPABILITIES: dict[str, dict[str, dict]] = {
    "google": {
        "drive": {
            "required_scopes": ["https://www.googleapis.com/auth/drive.readonly"],
            "operations": ["list_files", "download_file", "get_metadata", "search"],
            "supports_webhooks": True,
        },
        "docs": {
            "required_scopes": ["https://www.googleapis.com/auth/documents.readonly"],
            "operations": ["read_document", "export_as_text", "export_as_html"],
            "supports_webhooks": False,
        },
        "sheets": {
            "required_scopes": ["https://www.googleapis.com/auth/spreadsheets.readonly"],
            "operations": ["read_sheet", "list_sheets", "get_values", "export_csv"],
            "supports_webhooks": False,
        },
    },
    "notion": {
        "pages": {
            "required_scopes": [],  # Notion uses workspace permissions, not OAuth scopes
            "operations": ["list_pages", "read_page", "get_blocks", "search"],
            "supports_webhooks": True,
        },
        "databases": {
            "required_scopes": [],
            "operations": ["list_databases", "query_database", "get_properties", "export_data"],
            "supports_webhooks": True,
        },
    },
    "github": {
        "repositories": {
            "required_scopes": ["repo"],
            "operations": ["list_repos", "get_repo", "list_files", "get_file_content"],
            "supports_webhooks": True,
        },
        "issues": {
            "required_scopes": ["repo"],
            "operations": ["list_issues", "get_issue", "search_issues"],
            "supports_webhooks": True,
        },
        "pull_requests": {
            "required_scopes": ["repo"],
            "operations": ["list_prs", "get_pr", "get_pr_files", "get_pr_comments"],
            "supports_webhooks": True,
        },
    },
}

SUPPORTED_PROVIDERS = tuple(DATA_SOURCE_CAPABILITIES.keys())


def is_supported(provider: str) -> bool:
    return provider in DATA_SOURCE_CAPABILITIES


def data_sources_for(provider: str) -> dict[str, dict]:
    """The data sources a provider exposes (empty if the provider is unknown)."""
    return DATA_SOURCE_CAPABILITIES.get(provider, {})


def required_scopes_for(provider: str, data_source: str | None = None) -> list[str]:
    """Scopes required for one data source, or the union across all of a provider's data sources."""
    sources = data_sources_for(provider)
    if data_source is not None:
        return list(sources.get(data_source, {}).get("required_scopes", []))
    out: list[str] = []
    for spec in sources.values():
        for scope in spec.get("required_scopes", []):
            if scope not in out:
                out.append(scope)
    return out
