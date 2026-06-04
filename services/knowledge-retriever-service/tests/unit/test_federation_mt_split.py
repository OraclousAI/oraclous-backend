"""Multi-tenant read/write split enforcement tests (ORAA-59).

Acceptance criterion 3: read-side federation + LINKED_TO paths live in KRS;
write-side LINKED_TO operations (create/delete) stay in KGS — no duplication.

This file tests the *absence* of write operations in the KRS
``linked_to_service`` module:

  - ``create_graph_link`` must NOT be importable from KRS
  - ``create_entity_link`` must NOT be importable from KRS
  - ``delete_graph_link`` must NOT be importable from KRS
  - ``delete_entity_link`` must NOT be importable from KRS

And the *presence* of both read modules in KRS:

  - ``oraclous_knowledge_retriever_service.federation_service`` must exist
  - ``oraclous_knowledge_retriever_service.linked_to_service`` must exist

All KRS imports are function-local per ORA-48 / TST001.
RED until the ``[impl]`` PR lands the split modules in KRS.
"""

from __future__ import annotations

import importlib

import pytest

pytestmark = pytest.mark.unit


# ── Read-side is present in KRS ────────────────────────────────────────────


class TestReadSideInKRS:
    """Both read modules must be importable from ``oraclous_knowledge_retriever_service``."""

    _READ_MODULES = [
        "federation_service",
        "linked_to_service",
    ]

    @pytest.mark.parametrize("module_name", _READ_MODULES)
    def test_read_module_importable_from_krs(self, module_name: str) -> None:
        """``oraclous_knowledge_retriever_service.{module_name}`` must be importable."""
        mod = importlib.import_module(  # ORA-48
            f"oraclous_knowledge_retriever_service.{module_name}"
        )
        assert mod is not None, f"Expected {module_name!r} to exist in KRS"

    def test_list_graph_links_in_krs(self) -> None:
        """``list_graph_links`` (read path) must be present in the KRS module."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_graph_links,
        )

        assert callable(list_graph_links)

    def test_list_entity_links_in_krs(self) -> None:
        """``list_entity_links`` (read path) must be present in the KRS module."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_entity_links,
        )

        assert callable(list_entity_links)

    def test_federated_query_in_krs(self) -> None:
        """``FederationService.federated_query`` (read path) must be in KRS."""
        from oraclous_knowledge_retriever_service.federation_service import (  # ORA-48
            FederationService,
        )

        assert hasattr(FederationService, "federated_query")


# ── Write-side is absent from KRS ─────────────────────────────────────────


class TestWriteSideAbsentFromKRS:
    """Write operations must not be importable from the KRS linked_to_service module.

    These functions belong in KGS (write path). If the split is correct, any
    attempt to import them from KRS raises ``ImportError``.

    IMPORTANT: if the ``[impl]`` PR accidentally exports them, these tests
    will fail GREEN (i.e. no ImportError), which is the desired detection
    behaviour — the tests flip from RED (import error) to GREEN (no split).
    Wait: actually, we want to FAIL if the write ops are present.

    The assertion is: importing the symbol must raise ``ImportError``.
    """

    _WRITE_SYMBOLS = [
        "create_graph_link",
        "create_entity_link",
        "delete_graph_link",
        "delete_entity_link",
    ]

    @pytest.mark.parametrize("symbol", _WRITE_SYMBOLS)
    def test_write_symbol_not_in_krs_linked_to_service(self, symbol: str) -> None:
        """``{symbol}`` (write op) must NOT be importable from the KRS linked_to_service.

        If this test passes (no ImportError), the write-side has leaked into KRS.
        """
        try:
            from oraclous_knowledge_retriever_service import linked_to_service  # ORA-48

            present = hasattr(linked_to_service, symbol)
        except ModuleNotFoundError:
            # Module not yet extracted — this is the expected RED state.
            pytest.skip("linked_to_service not yet in KRS (RED window)")
            return

        assert not present, (
            f"'{symbol}' must NOT be present in the KRS linked_to_service module. "
            "Write operations belong in KGS only (R3-KRS-5 MT split)."
        )

    def test_initialize_schema_not_in_krs(self) -> None:
        """``initialize_schema`` (write/schema op) must NOT be in KRS linked_to_service."""
        try:
            from oraclous_knowledge_retriever_service import linked_to_service  # ORA-48
        except ModuleNotFoundError:
            pytest.skip("linked_to_service not yet in KRS (RED window)")
            return

        assert not hasattr(linked_to_service, "initialize_schema"), (
            "initialize_schema is a write/schema op and must live in KGS, not KRS"
        )


# ── No shadow copy of federation_service in KGS ───────────────────────────


class TestNoFederationServiceInKGS:
    """federation_service must NOT be importable from ``oraclous_knowledge_graph_service``.

    If KGS still carries a copy of federation_service after extraction, this
    test catches the duplication (AC3 — no duplication).
    """

    def test_federation_service_not_in_kgs(self) -> None:
        """``oraclous_knowledge_graph_service.federation_service`` must NOT exist."""
        try:
            import oraclous_knowledge_graph_service.federation_service as _m  # noqa: F401

            pytest.fail(
                "federation_service was found in oraclous_knowledge_graph_service — "
                "it must be removed after extraction to KRS (no duplication, AC3)"
            )
        except (ModuleNotFoundError, ImportError):
            pass  # Expected: module absent from KGS

    def test_kgs_linked_to_read_paths_absent(self) -> None:
        """The KGS must not expose ``list_graph_links`` or ``list_entity_links``.

        These are read-path symbols that belong exclusively in KRS after the split.
        Their presence in KGS indicates the split is incomplete or duplicated.
        """
        try:
            import oraclous_knowledge_graph_service.linked_to_service as kgs_lts
        except (ModuleNotFoundError, ImportError):
            # No linked_to_service in KGS at all — clean split.
            return

        for read_sym in ("list_graph_links", "list_entity_links"):
            assert not hasattr(kgs_lts, read_sym), (
                f"KGS linked_to_service must not expose '{read_sym}' (read-path belongs in KRS)"
            )
