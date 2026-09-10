"""Unit (#956, ruling 3): a connector's ``unsupported operation '…'`` message is bounded.

``f"unsupported operation '{operation}'"`` reflects whatever the caller put in ``operation`` into
an error message that the harness feeds back to the model and persists into the run transcript —
an unbounded echo channel. Four first-party connectors format it this way (github, postgresql,
mysql, notion); ``github_sink`` already says ``unsupported operation`` with no echo.

The imported-server path already has the guardrail: ``domain/connectors/mcp.py`` caps a tool's
own error text at ``_TOOL_ERROR_CHARS`` (300 — the run page's per-step budget, #697) with the
rationale written down. Ruling 3: the four connectors cap the echoed text at the SAME bound,
reused from one shared place, not re-derived per connector.

The shared name is a not-yet-built seam and is imported function-locally per
``.claude/rules/tests-seam-imports.md``; every test in this module is RED until the ``[impl]``
lands (``TOOL_ERROR_CHARS`` does not exist on ``executors.base`` today, and the four messages are
unbounded).

The two DB connectors open their connection BEFORE reading the operation, so their driver's
``connect`` is replaced with a no-op connection — nothing else about them is faked.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from oraclous_capability_registry_service.domain.connectors import mysql as mysql_mod
from oraclous_capability_registry_service.domain.connectors import postgresql as pg_mod
from oraclous_capability_registry_service.domain.connectors.github import GitHubReader
from oraclous_capability_registry_service.domain.connectors.mcp import _tool_error_message
from oraclous_capability_registry_service.domain.connectors.mysql import MySQLReader
from oraclous_capability_registry_service.domain.connectors.notion import NotionReader
from oraclous_capability_registry_service.domain.connectors.postgresql import PostgreSQLReader
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext

pytestmark = [pytest.mark.unit, pytest.mark.security]

_PREFIX = "unsupported operation ''"  # the fixed text around the echoed value
_LONG = "".join(chr(ord("a") + i % 26) for i in range(5000))
_SHORT = "delete_repo"


def _bound() -> int:
    """The ONE shared bound — the seam the ``[impl]`` hoists out of ``mcp.py``."""
    from oraclous_capability_registry_service.domain.executors.base import TOOL_ERROR_CHARS

    return int(TOOL_ERROR_CHARS)


def _context(credential_type: str, payload: dict[str, str]) -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        credentials={credential_type: payload},
    )


class _NoopAsyncpgConnection:
    async def close(self) -> None:
        pass


class _NoopAiomysqlConnection:
    def cursor(self, *_: Any) -> Any:
        @asynccontextmanager
        async def _cursor() -> Any:
            yield object()

        return _cursor()

    def close(self) -> None:
        pass


async def _github(operation: str) -> Any:
    reader = GitHubReader({"id": "github-reader"})
    reader.transport = httpx.MockTransport(lambda _: httpx.Response(500))  # must never be reached
    return await reader._execute_internal(
        {"operation": operation, "repo": "a/b"}, _context("api_key", {"api_key": "ghp_x"})
    )


async def _notion(operation: str) -> Any:
    reader = NotionReader({"id": "notion-reader"})
    reader.transport = httpx.MockTransport(lambda _: httpx.Response(500))
    return await reader._execute_internal(
        {"operation": operation}, _context("api_key", {"api_key": "ntn_x"})
    )


async def _postgresql(operation: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    async def _connect(*_: Any, **__: Any) -> _NoopAsyncpgConnection:
        return _NoopAsyncpgConnection()

    monkeypatch.setattr(pg_mod.asyncpg, "connect", _connect)
    reader = PostgreSQLReader({"id": "postgresql-reader"})
    return await reader._execute_internal(
        {"operation": operation},
        _context("connection_string", {"connection_string": "postgresql://u:p@h/db"}),
    )


async def _mysql(operation: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    async def _connect(*_: Any, **__: Any) -> _NoopAiomysqlConnection:
        return _NoopAiomysqlConnection()

    monkeypatch.setattr(mysql_mod.aiomysql, "connect", _connect)
    reader = MySQLReader({"id": "mysql-reader"})
    return await reader._execute_internal(
        {"operation": operation},
        _context("connection_string", {"connection_string": "mysql://u:p@h/db"}),
    )


async def _run(connector: str, operation: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    runners: dict[str, Callable[..., Any]] = {
        "github": lambda op: _github(op),
        "notion": lambda op: _notion(op),
        "postgresql": lambda op: _postgresql(op, monkeypatch),
        "mysql": lambda op: _mysql(op, monkeypatch),
    }
    return await runners[connector](operation)


_CONNECTORS = ["github", "notion", "postgresql", "mysql"]


# --- the shared bound is the mcp bound, hoisted, not a second number ------------------------------


def test_the_shared_bound_is_the_one_mcp_already_caps_at() -> None:
    """Reuse means ONE number: the mcp connector's own cap must be the shared constant, so the
    two paths cannot drift apart by someone editing one of them."""
    bound = _bound()
    capped = _tool_error_message([{"type": "text", "text": _LONG}])
    assert capped == f"the MCP tool reported a failure: {_LONG[:bound]}"


# --- each first-party connector caps the echoed operation at that bound ---------------------------


@pytest.mark.parametrize("connector", _CONNECTORS)
async def test_a_long_operation_is_echoed_no_further_than_the_bound(
    connector: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _run(connector, _LONG, monkeypatch)

    assert result.success is False
    assert result.error_type == "INVALID_OPERATION"
    assert result.error_message.startswith("unsupported operation")
    assert _LONG not in result.error_message, f"{connector}: the whole value is echoed"
    assert len(result.error_message) <= len(_PREFIX) + _bound(), (
        f"{connector}: {len(result.error_message)} chars, bound is {_bound()}"
    )


@pytest.mark.parametrize("connector", _CONNECTORS)
async def test_a_short_operation_is_still_named_so_the_message_stays_actionable(
    connector: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is a cap, not a blanket redaction: a member that typo'd ``list_file`` must still
    be told which name was wrong (#692's lesson — an unactionable error gets repeated)."""
    result = await _run(connector, _SHORT, monkeypatch)

    assert result.success is False
    assert result.error_type == "INVALID_OPERATION"
    assert _SHORT in result.error_message
