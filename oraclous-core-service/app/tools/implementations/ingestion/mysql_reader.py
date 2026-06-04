from __future__ import annotations

from app.models.capability_descriptor import DescriptorKind
from app.tools.plugin import CapabilityKindPlugin, plugin_registry


class MySQLReader(CapabilityKindPlugin):
    @classmethod
    def get_ohm_descriptor(cls) -> dict:
        return {
            "kind": "tool",
            "id": "mysql-reader",
            "version": {"hash": "sha256:0", "tags": []},
            "metadata": {
                "name": "MySQL Reader",
                "description": "Query and read data from MySQL databases.",
            },
            "spec": {
                "implementation": {
                    "type": "internal",
                    "handler": "app.tools.implementations.ingestion.mysql_reader.MySQLReader",
                },
                "input_schema": {},
                "output_schema": {},
                "credential_requirements": [
                    {
                        "type": "connection_string",
                        "provider": "mysql",
                    }
                ],
            },
        }

    @classmethod
    def get_kind(cls) -> DescriptorKind:
        return DescriptorKind.TOOL

    @classmethod
    def get_plugin_id(cls) -> str:
        return "mysql-reader"


plugin_registry.register(MySQLReader)
