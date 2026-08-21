#!/usr/bin/env python
"""Build the desk research team's manifest + sub_harnesses documents (#851).

``manifest.json`` in this directory is the committed source of truth: a hand-authored OHM v1.1
Team Harness with a fixed ``metadata.id`` and a placeholder ``owner_organization_id``. This module
loads it, builds each agent member's sub-harness via the SAME ``build_subharness`` the platform's
own importer uses (so the capability refs are exactly what a real import would produce), and binds
the real owning org into both documents. It does NOT call the network — see
``scripts/register_desk_team.py`` for that.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from oraclous_ohm.import_.mapping import build_subharness

_HERE = Path(__file__).parent
_MANIFEST_PATH = _HERE / "manifest.json"
_PLACEHOLDER_ORG = "00000000-0000-0000-0000-000000000000"


def load_manifest_template() -> dict[str, Any]:
    return json.loads(_MANIFEST_PATH.read_text())


def build_documents(owner_organization_id: uuid.UUID) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(manifest_document, sub_harnesses)`` with the org bound in, ready to POST as the
    ``CreateTeamDraftRequest`` body (``{name, manifest, sub_harnesses}``)."""
    manifest = load_manifest_template()
    manifest["metadata"]["owner_organization_id"] = str(owner_organization_id)

    sub_harnesses: dict[str, Any] = {}
    for member in manifest["members"]:
        if member["kind"] != "agent":
            continue
        role = member["role"]
        sub = build_subharness(
            role,
            owner_organization_id=owner_organization_id,
            body=member["subgoal"],
            tools=member.get("tools", []),
            description=f"The desk research team's {role} (#851).",
        )
        sub_harnesses[role] = sub.model_dump(mode="json")
    return manifest, sub_harnesses


if __name__ == "__main__":
    doc, subs = build_documents(uuid.UUID(_PLACEHOLDER_ORG))
    print(json.dumps({"manifest": doc, "sub_harnesses": subs}, indent=2))
