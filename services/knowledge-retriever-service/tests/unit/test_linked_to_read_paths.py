"""LINKED_TO read-path extraction tests (ORAA-59).

Acceptance criteria 1 & 2: ``oraclous_knowledge_retriever_service.linked_to_service``
exposes the read-side of the LINKED_TO primitive; ReBAC visibility enforcement
(ADR-021 §4 min_role filter) is applied on every list call.

Write operations (create_graph_link, create_entity_link, delete_graph_link,
delete_entity_link) must NOT be present in this module — those stay in KGS.
The no-write-ops contract is validated in
``test_federation_mt_split.py`` (AC3).

All imports of the not-yet-built seam are function-local per ORA-48 / TST001:
collection succeeds, each test fails RED at runtime with ``ModuleNotFoundError``
until the ``[impl]`` PR extracts the read paths into KRS.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

pytestmark = pytest.mark.unit

# ── Role hierarchy (mirrors _SYSTEM_ROLES from rebac_service) ─────────────

_SYSTEM_ROLES = ["owner", "editor", "viewer", "restricted_viewer", "denied"]
# Lower index = more privileged; e.g. owner (0) can see links with min_role
# any of the 5 roles; denied (4) can see nothing.

# ── Async driver fakes ─────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, Any]]:
        return list(self._rows)

    async def single(self) -> _FakeRecord | None:
        return _FakeRecord(self._rows[0]) if self._rows else None

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for row in self._rows:
            yield _FakeRecord(row)


class _FakeRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]


class _FakeSession:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self._responses = iter(responses)
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, params: dict[str, Any] | None = None) -> _FakeResult:
        self.queries.append((query, params or {}))
        try:
            rows = next(self._responses)
        except StopIteration:
            rows = []
        return _FakeResult(rows)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass


class _FakeDriver:
    """Vends sequential fake sessions from a list of response batches."""

    def __init__(self, *response_batches: list[dict[str, Any]]) -> None:
        self._batches = list(response_batches)
        self._idx = 0

    def session(self, **_kwargs: Any) -> _FakeSession:
        if self._idx < len(self._batches):
            batch = [self._batches[self._idx]]
            self._idx += 1
        else:
            batch = [[]]
        return _FakeSession(batch)


def _link_row(
    src: str,
    tgt: str,
    min_role: str = "viewer",
    created_by: str = "user-x",
) -> dict[str, Any]:
    return {
        "source_graph_id": src,
        "target_graph_id": tgt,
        "min_role": min_role,
        "created_by": created_by,
        "created_at": datetime.now(tz=UTC),
    }


def _entity_link_row(
    src_g: str,
    src_e: str,
    tgt_g: str,
    tgt_e: str,
    min_role: str = "viewer",
) -> dict[str, Any]:
    return {
        "source_graph_id": src_g,
        "source_entity_id": src_e,
        "target_graph_id": tgt_g,
        "target_entity_id": tgt_e,
        "min_role": min_role,
        "created_by": "user-x",
        "created_at": datetime.now(tz=UTC),
    }


# ── Module importability ───────────────────────────────────────────────────


class TestLinkedToReadModuleImport:
    """``oraclous_knowledge_retriever_service.linked_to_service`` is importable."""

    def test_linked_to_service_module_is_importable(self) -> None:
        """The linked_to_service module must be importable from the KRS package."""
        from oraclous_knowledge_retriever_service import (  # ORA-48
            linked_to_service,
        )

        assert linked_to_service is not None

    def test_list_graph_links_is_importable(self) -> None:
        """``list_graph_links`` must be importable directly."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_graph_links,
        )

        assert callable(list_graph_links)

    def test_list_entity_links_is_importable(self) -> None:
        """``list_entity_links`` must be importable directly."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_entity_links,
        )

        assert callable(list_entity_links)


# ── ReBAC visibility: graph-level links ───────────────────────────────────


class TestListGraphLinksReBAC:
    """list_graph_links applies ADR-021 §4 visibility: role >= min_role."""

    @pytest.mark.asyncio
    async def test_no_role_returns_empty(self) -> None:
        """A caller with no ReBAC role on the source graph sees no links."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_graph_links,
        )

        # First call: _user_role_level → no role found (empty)
        # Second call: link query (should not be reached — but we provide rows)
        driver = _FakeDriver(
            [],  # role lookup → no row
            [_link_row("g-src", "g-tgt", min_role="viewer")],  # link rows (should be hidden)
        )

        result = await list_graph_links(driver, "g-src", "user-no-role")
        assert result == [], f"Expected empty list for user with no role, got {result}"

    @pytest.mark.asyncio
    async def test_owner_sees_all_min_roles(self) -> None:
        """An owner (role index 0) can see links with any min_role."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_graph_links,
        )

        role_row = [{"role_names": ["owner"]}]
        link_rows = [
            _link_row("g-src", "g-tgt-1", min_role="owner"),
            _link_row("g-src", "g-tgt-2", min_role="editor"),
            _link_row("g-src", "g-tgt-3", min_role="viewer"),
            _link_row("g-src", "g-tgt-4", min_role="restricted_viewer"),
        ]
        driver = _FakeDriver(role_row, link_rows)

        result = await list_graph_links(driver, "g-src", "user-owner")
        assert len(result) == 4, f"Owner must see all 4 links, got {len(result)}"

    @pytest.mark.asyncio
    async def test_viewer_hidden_from_owner_min_role(self) -> None:
        """A viewer (role index 2) cannot see a link with min_role='owner' (index 0)."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_graph_links,
        )

        role_row = [{"role_names": ["viewer"]}]
        link_rows = [
            _link_row("g-src", "g-hidden", min_role="owner"),  # hidden (viewer > owner)
            _link_row("g-src", "g-visible", min_role="viewer"),  # visible (viewer == viewer)
        ]
        driver = _FakeDriver(role_row, link_rows)

        result = await list_graph_links(driver, "g-src", "user-viewer")
        visible_targets = [r["target_graph_id"] for r in result]
        assert "g-visible" in visible_targets
        assert "g-hidden" not in visible_targets, (
            "Viewer must not see links whose min_role is more restrictive than viewer"
        )

    @pytest.mark.asyncio
    async def test_invalid_min_role_in_stored_link_is_hidden(self) -> None:
        """Links with an unrecognised min_role value must be hidden (fail-closed)."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_graph_links,
        )

        role_row = [{"role_names": ["owner"]}]
        link_rows = [
            _link_row("g-src", "g-tgt", min_role="superuser"),  # invalid role
        ]
        driver = _FakeDriver(role_row, link_rows)

        result = await list_graph_links(driver, "g-src", "user-owner")
        assert result == [], "Links with an invalid min_role must be hidden (fail-closed)"


# ── ReBAC visibility: entity-level links ──────────────────────────────────


class TestListEntityLinksReBAC:
    """list_entity_links applies ADR-021 §4 visibility against the source graph role."""

    @pytest.mark.asyncio
    async def test_no_role_on_source_graph_returns_empty(self) -> None:
        """A caller with no role on the source graph sees no entity links."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_entity_links,
        )

        driver = _FakeDriver(
            [],  # role lookup → no row
            [_entity_link_row("g-src", "e1", "g-tgt", "e2", min_role="viewer")],
        )

        result = await list_entity_links(driver, "g-src", "e1", "user-no-role")
        assert result == []

    @pytest.mark.asyncio
    async def test_editor_sees_editor_and_lower_min_role_links(self) -> None:
        """An editor (role index 1) sees links with min_role editor, viewer, restricted_viewer."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_entity_links,
        )

        role_row = [{"role_names": ["editor"]}]
        link_rows = [
            _entity_link_row("g-src", "e0", "g-tgt", "e-owner", min_role="owner"),  # hidden
            _entity_link_row("g-src", "e0", "g-tgt", "e-editor", min_role="editor"),  # visible
            _entity_link_row("g-src", "e0", "g-tgt", "e-viewer", min_role="viewer"),  # visible
            _entity_link_row(
                "g-src", "e0", "g-tgt", "e-restricted", min_role="restricted_viewer"
            ),  # visible
        ]
        driver = _FakeDriver(role_row, link_rows)

        result = await list_entity_links(driver, "g-src", "e0", "user-editor")
        visible = {r["target_entity_id"] for r in result}
        assert "e-editor" in visible
        assert "e-viewer" in visible
        assert "e-restricted" in visible
        assert "e-owner" not in visible, "Editor must not see owner-min_role links"

    @pytest.mark.asyncio
    async def test_returns_list_with_expected_keys(self) -> None:
        """Each returned entity link dict must carry the expected keys."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_entity_links,
        )

        required_keys = {
            "source_graph_id",
            "source_entity_id",
            "target_graph_id",
            "target_entity_id",
            "min_role",
            "created_by",
            "created_at",
        }
        role_row = [{"role_names": ["owner"]}]
        link_rows = [_entity_link_row("g-src", "e1", "g-tgt", "e2", min_role="viewer")]
        driver = _FakeDriver(role_row, link_rows)

        result = await list_entity_links(driver, "g-src", "e1", "user-owner")
        assert len(result) == 1
        missing = required_keys - result[0].keys()
        assert not missing, f"Entity link dict missing keys: {missing}"


# ── read-path return-type contracts ───────────────────────────────────────


class TestLinkedToReadReturnTypes:
    """list_graph_links and list_entity_links must always return lists."""

    @pytest.mark.asyncio
    async def test_list_graph_links_returns_list(self) -> None:
        """list_graph_links must return a list (even when empty)."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_graph_links,
        )

        driver = _FakeDriver([], [])
        result = await list_graph_links(driver, "g-src", "any-user")
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_list_entity_links_returns_list(self) -> None:
        """list_entity_links must return a list (even when empty)."""
        from oraclous_knowledge_retriever_service.linked_to_service import (  # ORA-48
            list_entity_links,
        )

        driver = _FakeDriver([], [])
        result = await list_entity_links(driver, "g-src", "e1", "any-user")
        assert isinstance(result, list)
