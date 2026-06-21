"""Unit: the ScriptIngestionConnector — runs curated loaders as guarded subprocesses (#487).

These exercise the REAL subprocess path (the synthetic loaders ship in the package). Checks:
a curated loader's JSON output is captured to ExecutionResult.data; an unknown loader_id is rejected
without spawning; a non-zero exit is a coarse LOADER_FAILED that NEVER echoes stderr; a hung loader
times out and is process-group-killed; oversized output is capped; non-JSON is LOADER_BAD_OUTPUT;
args must be a flat scalar map; the child env does not inherit registry secrets; and the curated
descriptor is registered + factory-resolvable.
"""

from __future__ import annotations

import uuid

import pytest
from oraclous_capability_registry_service.domain.connectors.script_ingestion import (
    ScriptIngestionConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext
from oraclous_capability_registry_service.domain.executors.factory import create_executor
from oraclous_capability_registry_service.domain.loaders.registry import get_loader
from oraclous_capability_registry_service.domain.plugins import plugin_registry
from oraclous_capability_registry_service.domain.plugins.builtin import ScriptIngestionPlugin

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("00000000-0000-0000-0000-0000000008a1")
_USER = uuid.UUID("00000000-0000-0000-0000-0000000008c5")


def _ctx(creds: dict | None = None) -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=_ORG,
        user_id=_USER,
        execution_id=uuid.uuid4(),
        credentials=creds or {},
    )


def _connector() -> ScriptIngestionConnector:
    return ScriptIngestionConnector({"id": "x"})


async def test_curated_loader_runs_and_returns_captured_records() -> None:
    res = await _connector().execute({"loader_id": "synthetic", "args": {"count": 3}}, _ctx())
    assert res.success, res.error_message
    assert res.data["loader_id"] == "synthetic" and res.data["exit_code"] == 0
    assert [r["title"] for r in res.data["records"]] == [
        "synthetic-row-1",
        "synthetic-row-2",
        "synthetic-row-3",
    ]
    assert res.metadata["record_count"] == 3


async def test_unknown_loader_id_is_rejected() -> None:
    res = await _connector().execute({"loader_id": "rm -rf /"}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_missing_loader_id_is_rejected() -> None:
    res = await _connector().execute({"args": {"count": 1}}, _ctx())
    assert not res.success and res.error_type == "INVALID_INPUT"


async def test_non_zero_exit_is_loader_failed_and_never_leaks_stderr() -> None:
    res = await _connector().execute({"loader_id": "synthetic-fail"}, _ctx())
    assert not res.success and res.error_type == "LOADER_FAILED"
    assert res.metadata["exit_code"] == 3
    blob = (res.error_message or "") + str(res.data) + str(res.metadata)
    assert "CANARY" not in blob and "/etc/passwd" not in blob  # stderr is never echoed


async def test_a_hanging_loader_times_out_and_is_killed() -> None:
    ex = _connector()
    ex.subprocess_timeout_s = 0.5  # the synthetic-slow loader sleeps 120s
    res = await ex.execute({"loader_id": "synthetic-slow"}, _ctx())
    assert not res.success and res.error_type == "LOADER_TIMEOUT"


async def test_oversized_output_is_capped() -> None:
    ex = _connector()
    ex.max_output_bytes = 64  # the synthetic loader emits far more than 64 bytes for count=50
    res = await ex.execute({"loader_id": "synthetic", "args": {"count": 50}}, _ctx())
    assert not res.success and res.error_type == "OUTPUT_TOO_LARGE"


async def test_non_json_output_is_bad_output() -> None:
    res = await _connector().execute({"loader_id": "synthetic-text"}, _ctx())
    assert not res.success and res.error_type == "LOADER_BAD_OUTPUT"


async def test_args_must_be_a_flat_scalar_map() -> None:
    res = await _connector().execute(
        {"loader_id": "synthetic", "args": {"count": {"nested": 1}}}, _ctx()
    )
    assert not res.success and res.error_type == "INVALID_INPUT"


def test_minimal_env_does_not_inherit_registry_secrets() -> None:
    env = _connector()._minimal_env(_ctx(), get_loader("synthetic"))
    assert set(env) == {"PATH", "LANG"}  # no DATABASE_URL / INTERNAL_SERVICE_KEY / etc.


def test_keyed_loader_env_carries_only_the_resolved_key() -> None:
    from oraclous_capability_registry_service.domain.loaders.registry import LoaderSpec

    keyed = LoaderSpec("keyed", "x.y", requires_api_key=True)
    env = _connector()._minimal_env(_ctx({"api_key": {"api_key": "sek-123"}}), keyed)
    assert env["LOADER_API_KEY"] == "sek-123" and "PATH" in env


def test_plugin_is_registered_and_factory_resolves_it() -> None:
    assert ScriptIngestionPlugin in set(plugin_registry.discover())
    executor = create_executor(ScriptIngestionPlugin.descriptor())
    assert isinstance(executor, ScriptIngestionConnector)
