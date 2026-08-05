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


# ── the described catalog (#713): the same tools, plus what each one does ─────────────────────────


class _FakeRowRegistry:
    """The registry seam as the described catalog consumes it — rows, not bare names."""

    def __init__(
        self, rows: list[dict[str, str]] | None = None, raises: Exception | None = None
    ) -> None:
        self._rows = rows or []
        self._raises = raises

    async def list_capability_rows(self) -> list[dict[str, str]]:
        if self._raises is not None:
            raise self._raises
        return self._rows


async def test_described_catalog_pairs_each_slug_with_its_description() -> None:
    from oraclous_execution_engine_service.services.compiler_run_service import (  # §4.1 seam
        surveyed_catalog_described,
    )

    described = await surveyed_catalog_described(
        _FakeRowRegistry([{"name": "GitHub Sink", "description": "Post a comment to GitHub."}])
    )
    by_name = {e["name"]: e for e in described}
    assert by_name["github-sink"]["description"] == "Post a comment to GitHub."  # slugged, paired


async def test_described_catalog_covers_the_same_tools_as_the_slug_catalog() -> None:
    """The two views must not drift: the drafter's menu and the gate's allowed set are the same
    tools, described or not."""
    from oraclous_execution_engine_service.services.compiler_run_service import (  # §4.1 seam
        surveyed_catalog_described,
    )

    rows = [{"name": "GitHub Sink", "description": "Post a comment to GitHub."}]
    described = await surveyed_catalog_described(_FakeRowRegistry(rows))
    assert [e["name"] for e in described] == draft_catalog(["GitHub Sink"])


async def test_a_seed_tool_carries_no_description_key() -> None:
    """``survey_catalog`` unions the seed inventory (bare slugs) with the live rows. A seed tool
    has no description — render the name alone rather than an empty field."""
    from oraclous_execution_engine_service.services.compiler_run_service import (  # §4.1 seam
        surveyed_catalog_described,
    )

    described = await surveyed_catalog_described(_FakeRowRegistry([]))
    assert described  # the seed inventory is non-empty
    assert all("description" not in e for e in described)


async def test_a_live_row_with_an_empty_description_carries_none_either() -> None:
    from oraclous_execution_engine_service.services.compiler_run_service import (  # §4.1 seam
        surveyed_catalog_described,
    )

    described = await surveyed_catalog_described(
        _FakeRowRegistry([{"name": "GitHub Sink", "description": ""}])
    )
    entry = next(e for e in described if e["name"] == "github-sink")
    assert "description" not in entry


async def test_described_catalog_no_client_is_seed_only() -> None:
    from oraclous_execution_engine_service.services.compiler_run_service import (  # §4.1 seam
        surveyed_catalog_described,
    )

    described = await surveyed_catalog_described(None)
    assert [e["name"] for e in described] == draft_catalog()


@pytest.mark.parametrize(
    "boom",
    [RegistryClientError("unreachable"), RegistryRejected(503, "down")],
)
async def test_described_catalog_degrades_to_seed_only_on_an_outage(boom: Exception) -> None:
    # the same fail-closed degrade as the slug catalog — a blip narrows the menu, never widens it.
    from oraclous_execution_engine_service.services.compiler_run_service import (  # §4.1 seam
        surveyed_catalog_described,
    )

    described = await surveyed_catalog_described(_FakeRowRegistry(raises=boom))
    assert [e["name"] for e in described] == draft_catalog()
