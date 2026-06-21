"""Guarded-subprocess primitives (domain layer) — the shared isolation helpers.

Both the script-ingestion connector (a curated loader) and the standard ``bash`` tool (a sandboxed
shell command) run untrusted-ish work as a child process. The isolation baseline is identical, so it
lives here once: RLIMIT memory/CPU/fds/procs/file-size via ``preexec_fn`` (Linux), a process-group
SIGKILL so children don't survive a bare ``proc.kill()``, a clean minimal env (the registry's own
secrets are NOT inherited by the child), and a capped concurrent stdout/stderr capture that can
never OOM the host. Pure helpers, no I/O of their own beyond spawning the child the caller hands.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

_MIB = 1024 * 1024
MINIMAL_PATH = "/usr/local/bin:/usr/bin:/bin"


def set_limits() -> None:  # pragma: no cover — runs in the forked child, pre-exec (Linux)
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


def killpg(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group (children survive a bare ``proc.kill()``)."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A CLEAN env (not ``os.environ``) so the registry's own secrets never reach the child."""
    env = {"PATH": MINIMAL_PATH, "LANG": "C.UTF-8"}
    if extra:
        env.update(extra)
    return env


async def capped_capture(
    proc: asyncio.subprocess.Process, max_output_bytes: int
) -> tuple[bytes, bytes, bool]:
    """Read stdout + stderr up to the cap (+1 to detect overshoot) CONCURRENTLY so the child can't
    deadlock on a full pipe. Returns ``(stdout, stderr, overflowed)`` — ``overflowed`` is True if
    EITHER stream blew the cap. Each stream is independently capped; the caller decides what to do
    with stderr (script-ingestion discards it; bash returns a capped tail)."""
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

    (stdout, out_over), (stderr, err_over) = await asyncio.gather(
        _read(proc.stdout, max_output_bytes),
        _read(proc.stderr, max_output_bytes),
    )
    with contextlib.suppress(Exception):
        await proc.wait()
    return stdout, stderr, out_over or err_over
