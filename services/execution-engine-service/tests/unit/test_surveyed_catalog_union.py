"""Surveyed catalog = seed inventory ∪ the LIVE registry (#638 concern 1) — unit, fake registry.

``draft_catalog(registered)`` unions the #596 seed inventory with the live registry names (so a
deployed connector ``GitHub Sink`` → slug ``github-sink`` is admissible to compile/refine);
``surveyed_catalog(registry)`` fetches those names and — on a registry outage — degrades to
seed-only (strictly fail-closed: a live tool is never admitted un-surveyed; a registry blip never
fails-open).
"""

from __future__ import annotations

import pytest
from oraclous_execution_engine_service.domain.compiler_onramp import draft_catalog
from oraclous_execution_engine_service.services.compiler_run_service import surveyed_catalog
from oraclous_execution_engine_service.services.registry_client import (
    RegistryClientError,
    RegistryRejected,
)

pytestmark = pytest.mark.unit


def test_draft_catalog_seed_only_when_no_live_names() -> None:
    seed = draft_catalog()
    assert isinstance(seed, list) and seed  # the seed inventory is non-empty
    assert "github-sink" not in seed  # the deployed connector is NOT a seed tool
    assert draft_catalog([]) == seed  # an empty union == seed-only


def test_draft_catalog_unions_and_slugs_the_live_names() -> None:
    seed = draft_catalog()
    united = draft_catalog(["GitHub Sink", "core/web-research@1"])
    assert "github-sink" in united  # a display name is slugged into the catalog
    assert "web-research" in united  # a ref is slugged too (@version + core/ stripped)
    assert set(seed) <= set(united)  # the seed inventory is preserved (union, not replace)
    assert united == sorted(set(united))  # de-duplicated + sorted (survey_catalog's contract)


class _FakeRegistry:
    def __init__(self, names: list[str] | None = None, raises: Exception | None = None) -> None:
        self._names = names or []
        self._raises = raises

    async def list_capabilities(self) -> list[str]:
        if self._raises is not None:
            raise self._raises
        return self._names


async def test_surveyed_catalog_unions_the_live_registry() -> None:
    catalog = await surveyed_catalog(_FakeRegistry(["GitHub Sink"]))
    assert "github-sink" in catalog and set(draft_catalog()) <= set(catalog)


async def test_surveyed_catalog_no_client_is_seed_only() -> None:
    assert await surveyed_catalog(None) == draft_catalog()


@pytest.mark.parametrize(
    "boom",
    [RegistryClientError("unreachable"), RegistryRejected(503, "down")],
)
async def test_surveyed_catalog_degrades_to_seed_only_on_a_registry_outage(
    boom: Exception,
) -> None:
    # fail-closed: a registry blip degrades to the seed catalog (a live tool is temporarily
    # invisible → rejected), NEVER fail-open (an unsurveyed tool is never admitted).
    catalog = await surveyed_catalog(_FakeRegistry(raises=boom))
    assert catalog == draft_catalog()
    assert "github-sink" not in catalog  # the live connector is not admitted during the outage
