"""
[tests] Read-through in-memory cache for ToolRegistryService — ORAA-72 / ORA-70

Story: [ORAA-72](/ORAA/issues/ORAA-72) — demote in-memory to read-through cache; delete tool_sync_service
Blocked-was: [ORAA-71](/ORAA/issues/ORAA-71) — single DB-backed registry must exist first
Architecture refs:
  - R2 release page: https://oraclous.atlassian.net/wiki/spaces/OP/pages/688482
  - Test Strategy:   https://oraclous.atlassian.net/wiki/spaces/OP/pages/720940

All imports below will fail with ImportError until the implementer creates:
  - app/services/caching_tool_registry.py  (CachingToolRegistry)

That ImportError is intentional — this file is written test-first (TDD / ADR-010).

Behaviours covered (acceptance criteria for ORAA-72):

  C01  cache-miss on get_tool delegates to the backing registry (DB is queried)
  C02  cache-hit on get_tool does NOT call the backing registry a second time
  C03  register_tool invalidates the cache entry for that tool_id
  C04  update_tool invalidates the cache entry for that tool_id
  C05  delete_tool invalidates the cache entry; next get_tool falls through to DB (returns None)
  C06  no code path returns a cache-only result when the backing registry says the tool is absent
  C07  list_tools is served from cache on the second call (backing not called twice for same args)
  C08  write-through: list_tools cache is invalidated on any write (register/update/delete)
  C09  cache entry expires after TTL; next get_tool after expiry falls through to DB again
  C10  integration — cache-miss+hit round-trip against a real DB session
  C11  integration — write invalidation against a real DB session
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# These imports will fail with ImportError until the implementer creates
# app/services/caching_tool_registry.py.  The ImportError IS the expected
# initial TDD failure under ADR-010.
# ---------------------------------------------------------------------------
from app.services.caching_tool_registry import CachingToolRegistry  # noqa: E402

from app.interfaces.tool_registry import BaseToolRegistry
from app.schemas.common import ToolCategory, ToolType
from app.schemas.tool_definition import ToolDefinition, ToolSchema


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_tool(tool_id: Optional[str] = None, name: str = "Test Tool") -> ToolDefinition:
    """Return a minimal valid ToolDefinition for fixture use."""
    return ToolDefinition(
        id=uuid.UUID(tool_id) if tool_id else uuid.uuid4(),
        name=name,
        description="A test tool for cache behaviour tests.",
        version="1.0.0",
        category=ToolCategory.INGESTION,
        type=ToolType.INTERNAL,
        input_schema=ToolSchema(type="object"),
        output_schema=ToolSchema(type="object"),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_backing(tool: Optional[ToolDefinition] = None) -> AsyncMock:
    """Return an AsyncMock spec'd to BaseToolRegistry with sensible defaults."""
    mock = AsyncMock(spec=BaseToolRegistry)
    mock.get_tool.return_value = tool
    mock.register_tool.return_value = True
    mock.update_tool.return_value = True
    mock.delete_tool.return_value = True
    mock.list_tools.return_value = [tool] if tool else []
    mock.search_tools.return_value = [tool] if tool else []
    return mock


# ---------------------------------------------------------------------------
# C01 — cache-miss delegates to backing
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_cache_miss_delegates_to_backing_registry():
    """C01: first get_tool call is a cache-miss; backing registry is queried exactly once."""
    tool = _make_tool()
    backing = _make_backing(tool)
    registry = CachingToolRegistry(backing=backing, ttl_seconds=60)

    result = await registry.get_tool(str(tool.id))

    backing.get_tool.assert_called_once_with(str(tool.id))
    assert result is not None
    assert result.id == tool.id


# ---------------------------------------------------------------------------
# C02 — cache-hit skips backing
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_cache_hit_does_not_call_backing_registry_a_second_time():
    """C02: second get_tool call is a cache-hit; backing is NOT queried again."""
    tool = _make_tool()
    backing = _make_backing(tool)
    registry = CachingToolRegistry(backing=backing, ttl_seconds=60)

    first = await registry.get_tool(str(tool.id))
    second = await registry.get_tool(str(tool.id))

    # backing.get_tool must have been called exactly once across both calls
    backing.get_tool.assert_called_once()
    assert first == second


# ---------------------------------------------------------------------------
# C03 — register_tool invalidates cache entry
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_register_tool_invalidates_cache_entry():
    """C03: after register_tool the cache entry is evicted; next get_tool re-hits backing."""
    tool = _make_tool()
    backing = _make_backing(tool)
    registry = CachingToolRegistry(backing=backing, ttl_seconds=60)

    # Populate cache
    await registry.get_tool(str(tool.id))
    assert backing.get_tool.call_count == 1

    # Write must invalidate
    await registry.register_tool(tool)

    # Next read must miss cache and go to backing
    await registry.get_tool(str(tool.id))
    assert backing.get_tool.call_count == 2


# ---------------------------------------------------------------------------
# C04 — update_tool invalidates cache entry
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_update_tool_invalidates_cache_entry():
    """C04: after update_tool the cache entry is evicted; next get_tool re-hits backing."""
    tool = _make_tool()
    backing = _make_backing(tool)
    registry = CachingToolRegistry(backing=backing, ttl_seconds=60)

    await registry.get_tool(str(tool.id))
    assert backing.get_tool.call_count == 1

    await registry.update_tool(str(tool.id), tool)

    await registry.get_tool(str(tool.id))
    assert backing.get_tool.call_count == 2


# ---------------------------------------------------------------------------
# C05 — delete_tool invalidates cache entry; subsequent get returns None
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_delete_tool_invalidates_cache_and_returns_none_on_next_get():
    """C05: delete_tool evicts the cache entry; subsequent get_tool returns None (DB miss)."""
    tool = _make_tool()
    backing = _make_backing(tool)
    registry = CachingToolRegistry(backing=backing, ttl_seconds=60)

    # Populate cache
    await registry.get_tool(str(tool.id))
    assert backing.get_tool.call_count == 1

    # Delete invalidates
    await registry.delete_tool(str(tool.id))

    # After deletion backing should return None
    backing.get_tool.return_value = None
    result = await registry.get_tool(str(tool.id))

    assert backing.get_tool.call_count == 2
    assert result is None


# ---------------------------------------------------------------------------
# C06 — no cache-only lookup path
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_cache_never_serves_stale_tool_absent_from_backing():
    """C06: if the backing says a tool is absent, the cache must not return it.

    Scenario: tool was in cache, then deleted from DB outside the cache's
    knowledge (e.g., direct DB write).  After TTL expiry the cache falls
    through to backing and must return None, not the stale entry.
    """
    tool = _make_tool()
    backing = _make_backing(tool)
    # Tiny TTL so we can expire it synchronously
    registry = CachingToolRegistry(backing=backing, ttl_seconds=0)

    # First call populates cache
    await registry.get_tool(str(tool.id))

    # Simulate out-of-band deletion: backing now returns None
    backing.get_tool.return_value = None

    # With TTL=0 the entry is immediately expired; next call must re-query backing
    result = await registry.get_tool(str(tool.id))

    assert result is None
    assert backing.get_tool.call_count == 2


# ---------------------------------------------------------------------------
# C07 — list_tools served from cache on second call
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_list_tools_cache_hit_skips_backing_on_second_call():
    """C07: list_tools result is cached; identical second call does not hit backing."""
    tool = _make_tool()
    backing = _make_backing(tool)
    registry = CachingToolRegistry(backing=backing, ttl_seconds=60)

    first = await registry.list_tools(category=None, limit=50, offset=0)
    second = await registry.list_tools(category=None, limit=50, offset=0)

    backing.list_tools.assert_called_once()
    assert first == second


# ---------------------------------------------------------------------------
# C08 — write invalidates list_tools cache
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_write_invalidates_list_tools_cache():
    """C08: any write (register/update/delete) must also evict the list_tools cache."""
    tool = _make_tool()
    backing = _make_backing(tool)
    registry = CachingToolRegistry(backing=backing, ttl_seconds=60)

    # Populate list cache
    await registry.list_tools(category=None, limit=50, offset=0)
    assert backing.list_tools.call_count == 1

    # Any write must invalidate list cache
    await registry.register_tool(_make_tool(name="Another Tool"))

    # Next list must re-hit backing
    await registry.list_tools(category=None, limit=50, offset=0)
    assert backing.list_tools.call_count == 2


# ---------------------------------------------------------------------------
# C09 — TTL expiry causes re-fetch
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_cache_entry_expires_after_ttl_and_re_fetches_from_backing():
    """C09: a cache entry past its TTL is treated as a miss; backing is queried again."""
    tool = _make_tool()
    backing = _make_backing(tool)
    # Zero-second TTL means every access is expired
    registry = CachingToolRegistry(backing=backing, ttl_seconds=0)

    await registry.get_tool(str(tool.id))
    await registry.get_tool(str(tool.id))

    # Both calls must reach backing because TTL=0 expires immediately
    assert backing.get_tool.call_count == 2


# ---------------------------------------------------------------------------
# C10 — integration: cache-miss→DB→hit round-trip against real DB
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_cache_miss_then_hit_against_real_db(async_session):
    """C10: integration — first get_tool hits real DB; second is served from cache.

    Uses pytest.monkeypatch on the backing's get_tool to count actual DB calls
    while still running real DB queries.
    """
    from app.services.tool_registry import ToolRegistryService

    backing = ToolRegistryService(async_session)
    registry = CachingToolRegistry(backing=backing, ttl_seconds=60)

    tool = _make_tool()
    # Register directly via backing to seed DB
    await backing.register_tool(tool)

    original_get = backing.get_tool.__func__ if hasattr(backing.get_tool, "__func__") else None
    call_count = {"n": 0}

    original_get_tool = backing.get_tool

    async def counting_get_tool(tool_id: str):
        call_count["n"] += 1
        return await original_get_tool(tool_id)

    with patch.object(backing, "get_tool", side_effect=counting_get_tool):
        first = await registry.get_tool(str(tool.id))
        second = await registry.get_tool(str(tool.id))

    # DB should have been hit only once
    assert call_count["n"] == 1, (
        f"Expected backing.get_tool called once (cache-hit on 2nd call), "
        f"got {call_count['n']}"
    )
    assert first is not None
    assert first.id == tool.id
    assert second == first


# ---------------------------------------------------------------------------
# C11 — integration: write invalidation against real DB
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_write_invalidation_against_real_db(async_session):
    """C11: integration — update_tool evicts the cache; get_tool re-reads updated row from DB."""
    from app.services.tool_registry import ToolRegistryService

    backing = ToolRegistryService(async_session)
    registry = CachingToolRegistry(backing=backing, ttl_seconds=60)

    tool = _make_tool(name="Original Name")
    await backing.register_tool(tool)

    # Warm the cache
    cached = await registry.get_tool(str(tool.id))
    assert cached is not None
    assert cached.name == "Original Name"

    # Update via cache (which must invalidate)
    updated_tool = tool.copy(update={"name": "Updated Name"})
    await registry.update_tool(str(tool.id), updated_tool)

    # Next get must bypass cache and read the updated row from DB
    fresh = await registry.get_tool(str(tool.id))
    assert fresh is not None
    assert fresh.name == "Updated Name", (
        "Cache was not invalidated after update_tool; stale name returned."
    )
