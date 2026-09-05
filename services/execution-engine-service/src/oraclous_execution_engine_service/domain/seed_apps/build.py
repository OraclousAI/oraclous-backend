"""Build the Oraclous-provided apps' documents (domain layer, #932).

Lifted from ``scripts/desk_research_team/build.py``, which this replaces. Two things changed in the
move, and both matter:

* the owning organisation is the PLATFORM organisation, not whichever throwaway tenant a script had
  registered as. That answers the open question in ``docs/specs/851-desk-research-team.md`` — the
  platform organisation owns the team.
* the sub-harnesses are kept INLINE on the app rather than filed into the capability registry. A
  filed agent is org-scoped and editable in place, so resolving one would make every tenant's run of
  a shared app depend on a cross-organisation registry read. Inline documents make the app
  self-contained: ``_resolve_member_manifests`` short-circuits on them and resolves nothing.

Each member's sub-harness is still built by the SAME ``build_subharness`` the platform's own
importer uses, so the capability refs are exactly what a real import would produce.

No network, no credentials. A seeded app carries no key of any kind — which is also what forces a
run onto the caller's own key, since the app has nothing else to run on.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oraclous_ohm.import_.mapping import build_subharness

_HERE = Path(__file__).parent


@dataclass(frozen=True)
class SeedApp:
    """One Oraclous-provided app, ready to store."""

    #: Fixed, so the console's deep links survive every redeploy. A fresh id per boot would break
    #: every saved link.
    app_id: uuid.UUID
    slug: str
    name: str
    description: str
    manifest: dict[str, Any]
    sub_harnesses: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class _SeedSpec:
    app_id: uuid.UUID
    slug: str
    directory: str
    name: str
    description: str


#: The apps Oraclous provides. One today; the list is the extension point, so adding a second is a
#: new entry rather than new machinery.
_SEEDS: tuple[_SeedSpec, ...] = (
    _SeedSpec(
        app_id=uuid.UUID("a99d0000-0000-4000-8000-00000000de5c"),
        slug="validation-desk",
        directory="validation_desk",
        name="Validation Desk",
        description=(
            "Gathers evidence for an idea, tries to break it, designs the cheapest test for what "
            "is left unsettled, and writes one decision brief."
        ),
    ),
)


def _load_manifest(directory: str) -> dict[str, Any]:
    return json.loads((_HERE / directory / "manifest.json").read_text())


def _stable_member_id(app_id: uuid.UUID, role: str) -> uuid.UUID:
    """A member document's id, derived from the app and the role rather than drawn fresh.

    ``build_subharness`` mints a random ``metadata.id`` per call, which is right for an import but
    wrong for a seed: the seed rebuilds these documents on every boot, so a random id would change
    the app's fingerprint every time, rewrite a live row nobody edited, and march
    ``pinned_version`` upward forever. Deriving the id makes the seed reproducible, which is what
    lets "has this actually changed?" be answered by comparing content.
    """
    return uuid.uuid5(app_id, role)


def build_seed_apps(platform_org_id: uuid.UUID) -> list[SeedApp]:
    """Every Oraclous-provided app, with the platform organisation bound into its documents."""
    built: list[SeedApp] = []
    for spec in _SEEDS:
        manifest = _load_manifest(spec.directory)
        manifest["metadata"]["owner_organization_id"] = str(platform_org_id)

        sub_harnesses: dict[str, dict[str, Any]] = {}
        for member in manifest["members"]:
            if member["kind"] != "agent":
                continue
            role = member["role"]
            sub = build_subharness(
                role,
                owner_organization_id=platform_org_id,
                body=member["subgoal"],
                tools=member.get("tools", []),
                description=f"{spec.name}: the {role}.",
            )
            document = sub.model_dump(mode="json")
            document["metadata"]["id"] = str(_stable_member_id(spec.app_id, role))
            sub_harnesses[role] = document

        built.append(
            SeedApp(
                app_id=spec.app_id,
                slug=spec.slug,
                name=spec.name,
                description=spec.description,
                manifest=manifest,
                sub_harnesses=sub_harnesses,
            )
        )
    return built
