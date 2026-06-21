"""Script-ingestion connector (ORAA-4 §21 domain layer) — adopt a loader as a registry tool (#487).

Runs a CURATED loader (selected by ``loader_id`` from :mod:`domain.loaders.registry`, never a free
argv) as a subprocess inside the registry container, captures its stdout, and returns it as the
execution's ``output_data`` — which ``execute_sync`` finalizes onto the org-scoped (RLS) Execution
row, i.e. the loader's output lands in the org store (ADR-038 D1). The cron that fires this on a
cadence is #489; #487 ships the manual-dispatch executor that #489 will schedule.

ADR-038 D5 isolation (baseline, in-process): no shell (``create_subprocess_exec`` — no injection);
RLIMIT memory/CPU/fds/procs/file-size via ``preexec_fn`` (Linux); a hard inner timeout that SIGKILLs
the whole process group (``start_new_session`` + ``os.killpg``); an 8 MiB output cap (no OOM); a
clean minimal env (the registry's own secrets are NOT inherited); and full no-leak error mapping
(an arbitrary loader's stderr is NEVER echoed). True namespace/egress isolation + user-supplied
loader adoption are tracked follow-ups (curated loaders only ship here).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from typing import Any

from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)
from oraclous_capability_registry_service.domain.loaders.registry import LoaderSpec, get_loader

_MIB = 1024 * 1024
_MINIMAL_PATH = "/usr/local/bin:/usr/bin:/bin"


def _set_limits() -> None:  # pragma: no cover — runs in the forked child, pre-exec (Linux)
    """preexec_fn: cap the child's memory/CPU/fds/procs/file-size. Best-effort across platforms."""
    import resource

    def _try(which: int, soft: int, hard: int | None = None) -> None:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(which, (soft, hard if hard is not None else soft))

    _try(resource.RLIMIT_AS, 512 * _MIB)  # address space / memory
    _try(resource.RLIMIT_CPU, 10)  # CPU seconds (soft; the asyncio timeout is authoritative)
    _try(resource.RLIMIT_NOFILE, 64)  # open file descriptors
    _try(resource.RLIMIT_FSIZE, 16 * _MIB)  # max bytes written to any single file
    if hasattr(resource, "RLIMIT_NPROC"):
        _try(resource.RLIMIT_NPROC, 64)  # blunt fork-bombs (not on every platform)


def _killpg(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group (children survive a bare ``proc.kill()``)."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


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
        env = {"PATH": _MINIMAL_PATH, "LANG": "C.UTF-8"}
        if spec.requires_api_key:
            creds = self.get_credentials(context, "api_key")
            if isinstance(creds, dict) and creds.get("api_key"):
                env["LOADER_API_KEY"] = str(creds["api_key"])
        return env

    async def _run(
        self, command: list[str], env: dict[str, str], loader_id: str
    ) -> ExecutionResult:
        preexec = _set_limits if sys.platform != "win32" else None
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
            stdout, over = await asyncio.wait_for(
                self._capped_capture(proc), self.subprocess_timeout_s
            )
        except TimeoutError:
            _killpg(proc)
            return ExecutionResult(
                success=False,
                error_message="the loader exceeded its time budget",
                error_type="LOADER_TIMEOUT",
                metadata={"loader_id": loader_id},
            )
        if over:
            _killpg(proc)
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

    async def _capped_capture(self, proc: asyncio.subprocess.Process) -> tuple[bytes, bool]:
        """Read stdout up to the cap (+1 to detect overshoot) and DRAIN stderr concurrently so the
        child can't deadlock on a full stderr pipe. stderr is read but discarded (never echoed)."""
        assert proc.stdout is not None and proc.stderr is not None  # noqa: S101 — PIPE always set

        async def _read(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return b"".join(chunks), False
                total += len(chunk)
                if total > limit:
                    return b"".join(chunks), True
                chunks.append(chunk)

        (stdout, over), _ = await asyncio.gather(
            _read(proc.stdout, self.max_output_bytes),
            _read(proc.stderr, self.max_output_bytes),
        )
        with contextlib.suppress(Exception):
            await proc.wait()
        return stdout, over
