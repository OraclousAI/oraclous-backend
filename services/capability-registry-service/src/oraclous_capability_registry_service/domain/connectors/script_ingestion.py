"""Script-ingestion connector (domain layer) — adopt a loader as a registry tool (#487).

Runs a CURATED loader (selected by ``loader_id`` from :mod:`domain.loaders.registry`, never a free
argv) as a subprocess inside the registry container, captures its stdout, and returns it as the
execution's ``output_data`` — which ``execute_sync`` finalizes onto the org-scoped (RLS) Execution
row, i.e. the loader's output lands in the org store (ADR-038 D1). The cron that fires this on a
cadence is #489; #487 ships the manual-dispatch executor that #489 will schedule.

ADR-038 D5 isolation (baseline, in-process): no shell (``create_subprocess_exec`` — no injection);
RLIMIT memory/CPU/fds/procs/file-size via ``preexec_fn`` (Linux); a hard inner timeout that SIGKILLs
the whole process group (``start_new_session`` + ``os.killpg``); an 8 MiB output cap (no OOM); a
clean minimal env (the registry's own secrets are NOT inherited); and full no-leak error mapping
(an arbitrary loader's stderr is NEVER echoed). The isolation primitives are shared with the
standard ``bash`` tool via :mod:`domain.subprocess_guard`. True namespace/egress isolation +
user-supplied loader adoption are tracked follow-ups (curated loaders only ship here).
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)
from oraclous_capability_registry_service.domain.loaders.registry import LoaderSpec, get_loader
from oraclous_capability_registry_service.domain.subprocess_guard import (
    capped_capture,
    killpg,
    minimal_env,
    set_limits,
)

_MIB = 1024 * 1024


class ScriptIngestionConnector(InternalTool):
    """Runs a curated loader as a guarded subprocess and returns its JSON output (#487)."""

    #: outer InternalTool hard timeout (asyncio.wait_for wrapper) — the authoritative bound.
    timeout_s: float = 60.0
    #: inner subprocess cap; sits UNDER ``timeout_s`` so a hung loader surfaces LOADER_TIMEOUT.
    subprocess_timeout_s: float = 45.0
    #: hard cap on captured stdout — overshoot is OUTPUT_TOO_LARGE, never an OOM.
    max_output_bytes: int = 8 * _MIB

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        loader_id = input_data.get("loader_id")
        if not isinstance(loader_id, str) or not loader_id.strip():
            return ExecutionResult(
                success=False, error_message="'loader_id' is required", error_type="INVALID_INPUT"
            )
        spec = get_loader(loader_id)
        if spec is None:
            return ExecutionResult(
                success=False,
                error_message=f"'{loader_id}' is not a known curated loader",
                error_type="INVALID_INPUT",
            )
        argv = self._build_argv(input_data.get("args"))
        if argv is None:
            return ExecutionResult(
                success=False,
                error_message="'args' must be a flat map of string/number values",
                error_type="INVALID_INPUT",
            )
        try:
            command = spec.command(argv)
        except LookupError:
            return ExecutionResult(
                success=False,
                error_message="the loader is unavailable",
                error_type="LOADER_UNAVAILABLE",
            )
        return await self._run(command, self._minimal_env(context, spec), loader_id)

    @staticmethod
    def _build_argv(args: Any) -> list[str] | None:
        """Build argv from a flat ``{name: scalar}`` map only; reject nested/odd values."""
        if args is None:
            return []
        if not isinstance(args, dict):
            return None
        out: list[str] = []
        for key, value in args.items():
            if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)):
                return None
            out.append(f"--{key}")
            out.append(str(value))
        return out

    def _minimal_env(self, context: ExecutionContext, spec: LoaderSpec) -> dict[str, str]:
        """A CLEAN env (not ``os.environ``) so the registry's own secrets never reach the child."""
        extra: dict[str, str] = {}
        if spec.requires_api_key:
            creds = self.get_credentials(context, "api_key")
            if isinstance(creds, dict) and creds.get("api_key"):
                extra["LOADER_API_KEY"] = str(creds["api_key"])
        return minimal_env(extra)

    async def _run(
        self, command: list[str], env: dict[str, str], loader_id: str
    ) -> ExecutionResult:
        preexec = set_limits if sys.platform != "win32" else None
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                preexec_fn=preexec,
                start_new_session=True,
            )
        except OSError:
            return ExecutionResult(
                success=False,
                error_message="the loader could not be started",
                error_type="LOADER_UNAVAILABLE",
            )
        try:
            # stderr is captured but DISCARDED — an arbitrary loader's stderr can carry secrets.
            stdout, _stderr, over = await asyncio.wait_for(
                capped_capture(proc, self.max_output_bytes), self.subprocess_timeout_s
            )
        except TimeoutError:
            killpg(proc)
            return ExecutionResult(
                success=False,
                error_message="the loader exceeded its time budget",
                error_type="LOADER_TIMEOUT",
                metadata={"loader_id": loader_id},
            )
        if over:
            killpg(proc)
            return ExecutionResult(
                success=False,
                error_message="the loader produced too much output",
                error_type="OUTPUT_TOO_LARGE",
                metadata={"loader_id": loader_id},
            )
        rc = proc.returncode
        if rc != 0:
            # An arbitrary loader's stderr can carry secrets/paths — it is NEVER echoed.
            return ExecutionResult(
                success=False,
                error_message="the loader exited with a non-zero status",
                error_type="LOADER_FAILED",
                metadata={"loader_id": loader_id, "exit_code": rc},
            )
        try:
            parsed = json.loads(stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return ExecutionResult(
                success=False,
                error_message="the loader output was not valid JSON",
                error_type="LOADER_BAD_OUTPUT",
                metadata={"loader_id": loader_id},
            )
        data = parsed if isinstance(parsed, dict) else {"records": parsed}
        data["loader_id"] = loader_id
        data["exit_code"] = 0
        record_count = len(data["records"]) if isinstance(data.get("records"), list) else None
        return ExecutionResult(
            success=True, data=data, metadata={"loader_id": loader_id, "record_count": record_count}
        )
