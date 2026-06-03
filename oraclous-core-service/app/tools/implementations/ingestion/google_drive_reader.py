from __future__ import annotations

from app.models.capability_descriptor import DescriptorKind
from app.tools.plugin import CapabilityKindPlugin, plugin_registry

_HANDLER = "app.tools.implementations.ingestion.google_drive_reader.GoogleDriveReader"


class GoogleDriveReader(CapabilityKindPlugin):
    @classmethod
    def get_ohm_descriptor(cls) -> dict:
        return {
            "kind": "tool",
            "id": "google-drive-reader",
            "version": {"hash": "sha256:0", "tags": []},
            "metadata": {
                "name": "Google Drive Reader",
                "description": "Read files and documents from Google Drive.",
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
                        "type": "oauth_token",
                        "provider": "google",
                        "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
                    }
                ],
            },
        }

    @classmethod
    def get_kind(cls) -> DescriptorKind:
        return DescriptorKind.TOOL

    @classmethod
    def get_plugin_id(cls) -> str:
        return "google-drive-reader"


plugin_registry.register(GoogleDriveReader)
