from __future__ import annotations

from app.models.capability_descriptor import DescriptorKind
from app.tools.plugin import CapabilityKindPlugin, plugin_registry


class NotionReader(CapabilityKindPlugin):
    @classmethod
    def get_ohm_descriptor(cls) -> dict:
        return {
            "kind": "tool",
            "id": "notion-reader",
            "version": {"hash": "sha256:0", "tags": []},
            "metadata": {
                "name": "Notion Reader",
                "description": "Read pages and databases from Notion.",
            },
            "spec": {
                "implementation": {
                    "type": "internal",
                    "handler": "app.tools.implementations.ingestion.notion_reader.NotionReader",
                },
                "input_schema": {},
                "output_schema": {},
                "credential_requirements": [
                    {
                        "type": "api_key",
                        "provider": "notion",
                    }
                ],
            },
        }

    @classmethod
    def get_kind(cls) -> DescriptorKind:
        return DescriptorKind.TOOL

    @classmethod
    def get_plugin_id(cls) -> str:
        return "notion-reader"


plugin_registry.register(NotionReader)
