"""The surveyed catalog hides the file tools under the graph substrate (#694).

Filtering beats remapping at this layer: the drafter cannot pick what it is not shown, and no
rewrite step is needed afterwards. The remap in ``import_.mapping`` stays as the backstop for a
draft that carries a file tool anyway (an import, a hand edit, a team compiled before this fix).

Two halves, both required. Take the file tools away and the drafter has nothing to persist with;
``graph-ingest`` arrives in the seed inventory in the same slice
(``packages/ohm/tests/test_seeds_graph_ingest.py``). ``bash`` stays — it is the sandbox exec
fallback (#507), not a deliverable sink.

RED until the [impl] adds the ``substrate`` parameter to ``draft_catalog``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

_FILE_TOOLS = {"read", "write", "edit", "grep", "glob"}


def _catalog(registered: list[str] | None = None, **kw: str) -> list[str]:
    from oraclous_execution_engine_service.domain.compiler_onramp import draft_catalog

    return draft_catalog(registered, **kw)  # type: ignore[arg-type]


def test_the_graph_catalog_offers_no_file_tool() -> None:
    """The drafter's menu. Run ``fe548aac``'s surveyor returned ``write`` and ``edit`` in it."""
    assert _FILE_TOOLS.isdisjoint(_catalog())


def test_the_graph_catalog_offers_the_graph_write_side() -> None:
    """Otherwise the team can read the graph and persist nothing — the same outcome by a
    different route."""
    assert "graph-ingest" in _catalog()


def test_bash_survives_the_filter() -> None:
    """The rare exec need is not a deliverable sink and is not what #694 reports."""
    assert "bash" in _catalog()


def test_the_research_and_delivery_tools_are_untouched() -> None:
    catalog = _catalog()
    assert {"web-research", "websearch", "webfetch", "send-to-drafts"} <= set(catalog)


def test_the_file_substrate_still_offers_the_file_tools() -> None:
    """The parked local single-tenant mode is the reason the filter is a parameter, not a delete."""
    assert _FILE_TOOLS <= set(_catalog(substrate="file"))


def test_the_substrate_defaults_to_graph() -> None:
    """Cloud-first (ADR-040 Decision 7). Every existing caller passes no substrate today, so the
    default is what actually ships — a default of ``file`` would reintroduce the bug silently."""
    assert _FILE_TOOLS.isdisjoint(_catalog())


def test_a_live_registry_file_tool_is_filtered_too() -> None:
    """The union with the org's live registry (#638) must not smuggle a file tool back in. A
    connector legitimately named ``Write`` in some org would otherwise reopen this."""
    catalog = _catalog(["Write", "GitHub Sink"])
    assert "write" not in catalog
    assert "github-sink" in catalog  # the real connector still comes through


def test_the_described_catalog_filters_identically() -> None:
    """The menu a MODEL reads (#713) and the list a GATE diffs against must not drift — that
    divergence is exactly how #694 survived a validator."""
    from oraclous_execution_engine_service.domain.compiler_onramp import (
        draft_catalog,
        draft_catalog_described,
    )

    rows = [{"name": "Write", "description": "Write text to a file."}]
    described = draft_catalog_described(rows)
    assert [e["name"] for e in described] == draft_catalog(["Write"])
    assert all(e["name"] not in _FILE_TOOLS for e in described)


def test_the_described_catalog_honours_the_file_substrate_too() -> None:
    from oraclous_execution_engine_service.domain.compiler_onramp import draft_catalog_described

    described = draft_catalog_described([], substrate="file")
    assert _FILE_TOOLS <= {e["name"] for e in described}
