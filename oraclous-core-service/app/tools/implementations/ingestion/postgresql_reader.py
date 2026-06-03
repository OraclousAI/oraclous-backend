from __future__ import annotations

from app.models.capability_descriptor import DescriptorKind
from app.tools.plugin import CapabilityKindPlugin, plugin_registry

_HANDLER = "app.tools.implementations.ingestion.postgresql_reader.PostgreSQLReader"


class PostgreSQLReader(CapabilityKindPlugin):
    @classmethod
    def get_ohm_descriptor(cls) -> dict:
        return {
            "kind": "tool",
            "id": "postgresql-reader",
            "version": {"hash": "sha256:0", "tags": []},
            "metadata": {
                "name": "PostgreSQL Reader",
                "description": "Query and read data from PostgreSQL databases.",
            },
            "spec": {
                "implementation": {
                    "type": "internal",
                    "handler": _HANDLER,
                },
                "input_schema": {},
                "output_schema": {},
                "credential_requirements": [
                    {
                        "type": "connection_string",
                        "provider": "postgresql",
                    }
                ],
            },
        }

    @classmethod
    def get_kind(cls) -> DescriptorKind:
        return DescriptorKind.TOOL

    @classmethod
    def get_plugin_id(cls) -> str:
        return "postgresql-reader"


plugin_registry.register(PostgreSQLReader)
