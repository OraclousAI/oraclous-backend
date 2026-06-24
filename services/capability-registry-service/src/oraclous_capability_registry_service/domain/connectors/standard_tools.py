"""Standard agent toolset connectors (domain layer) — the curated built-in ``core/*`` tools.

An imported ``.claude/agents`` team (the #440 book team) declares the Claude-Code standard tools
(``Read``/``Grep``/``Glob``/``Write``/``Edit``/``Bash``/``WebSearch``/``WebFetch``); the E2 importer
maps each to a ``core/<slug>@1`` capability ref, and at member dispatch the registry resolves the
ref to a curated capability BY SLUG. Before this file none of those eight were registered, so
resolution failed (``OHMReferenceError`` → harness 422 → every member failed). These connectors are
the executors behind the eight curated descriptors in :mod:`domain.plugins.builtin`, so the team
resolves and runs instead of 422-ing.

Confinement: the file tools (Read/Write/Edit/Grep/Glob) and Bash's working directory are confined
to a per-org scratch sandbox (:mod:`domain.sandbox`) — an escaping path is refused fail-closed.
Bash runs as a guarded subprocess (RLIMIT + process-group SIGKILL + capped output + clean minimal
env, via :mod:`domain.subprocess_guard`). NONE of these eight is a publish/upload/spend/send tool
and the sandbox grants no host access — the capability ceiling stays exactly the standard eight.

WebSearch / WebFetch are NOT reimplemented: they delegate to :class:`WebResearchConnector`'s already
SSRF-guarded search/fetch path so the live-web behaviour (and its egress gate) is shared, not
forked.
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

from oraclous_capability_registry_service.domain.connectors.web_research import WebResearchConnector
from oraclous_capability_registry_service.domain.executors.base import (
    ExecutionContext,
    ExecutionResult,
    InternalTool,
)
from oraclous_capability_registry_service.domain.sandbox import (
    SandboxPathError,
    resolve_in_sandbox,
    sandbox_root,
)
from oraclous_capability_registry_service.domain.subprocess_guard import (
    capped_capture,
    killpg,
    minimal_env,
    set_limits,
)

_MAX_FILE_BYTES = 1024 * 1024  # 1 MiB read/write cap — a tool result, not a bulk transfer
_MAX_GREP_MATCHES = 500
_MAX_GLOB_RESULTS = 1000
_BASH_MAX_OUTPUT_BYTES = 1024 * 1024  # 1 MiB combined stdout/stderr cap
_BASH_TIMEOUT_S = 30.0
_BASH_OUTER_TIMEOUT_S = 35.0  # InternalTool wrapper; sits ABOVE the inner subprocess timeout


def _bad(message: str, error_type: str = "INVALID_INPUT") -> ExecutionResult:
    return ExecutionResult(success=False, error_message=message, error_type=error_type)


class ReadFileConnector(InternalTool):
    """``Read`` — read a UTF-8 text file from the org sandbox. Returns ``{content, path}``."""

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        try:
            path = resolve_in_sandbox(
                context.organisation_id, input_data.get("path", ""), context.working_dir
            )
        except SandboxPathError as exc:
            return _bad(str(exc))
        if not path.is_file():
            return _bad("file not found", error_type="NOT_FOUND")
        if path.stat().st_size > _MAX_FILE_BYTES:
            return _bad("file exceeds the read size limit", error_type="FILE_TOO_LARGE")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _bad("file is not valid UTF-8 text", error_type="NOT_TEXT")
        except OSError:
            return _bad("file could not be read", error_type="READ_FAILED")
        return ExecutionResult(success=True, data={"content": content, "path": input_data["path"]})


class WriteFileConnector(InternalTool):
    """``Write`` — write text to a sandbox path (dirs created). Returns ``{ok, path, bytes}``."""

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        content = input_data.get("content")
        if not isinstance(content, str):
            return _bad("'content' must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_FILE_BYTES:
            return _bad("content exceeds the write size limit", error_type="FILE_TOO_LARGE")
        try:
            path = resolve_in_sandbox(
                context.organisation_id, input_data.get("path", ""), context.working_dir
            )
        except SandboxPathError as exc:
            return _bad(str(exc))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError:
            return _bad("file could not be written", error_type="WRITE_FAILED")
        return ExecutionResult(
            success=True, data={"ok": True, "path": input_data["path"], "bytes": len(encoded)}
        )


class EditFileConnector(InternalTool):
    """``Edit`` — replace ``old_string`` with ``new_string`` in a sandbox file (must be unique)."""

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        old = input_data.get("old_string")
        new = input_data.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return _bad("'old_string' and 'new_string' must be strings")
        if not old:
            return _bad("'old_string' must be non-empty")
        try:
            path = resolve_in_sandbox(
                context.organisation_id, input_data.get("path", ""), context.working_dir
            )
        except SandboxPathError as exc:
            return _bad(str(exc))
        if not path.is_file():
            return _bad("file not found", error_type="NOT_FOUND")
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return _bad("file could not be read", error_type="READ_FAILED")
        count = content.count(old)
        if count == 0:
            return _bad("'old_string' not found in the file", error_type="NO_MATCH")
        if count > 1:
            return _bad("'old_string' is not unique in the file", error_type="AMBIGUOUS_MATCH")
        updated = content.replace(old, new, 1)
        if len(updated.encode("utf-8")) > _MAX_FILE_BYTES:
            return _bad("edit would exceed the file size limit", error_type="FILE_TOO_LARGE")
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError:
            return _bad("file could not be written", error_type="WRITE_FAILED")
        return ExecutionResult(
            success=True, data={"ok": True, "path": input_data["path"], "replacements": 1}
        )


class GrepConnector(InternalTool):
    """``Grep`` — regex-search the sandbox (bounded). Returns ``{matches: [{path,line,text}]}``."""

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        pattern = input_data.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return _bad("'pattern' is required")
        try:
            regex = re.compile(pattern)
        except re.error:
            return _bad("'pattern' is not a valid regular expression")
        root = sandbox_root(context.organisation_id, context.working_dir)
        sub = input_data.get("path")
        try:
            search_root = (
                resolve_in_sandbox(context.organisation_id, sub, context.working_dir)
                if isinstance(sub, str) and sub
                else root
            )
        except SandboxPathError as exc:
            return _bad(str(exc))
        matches: list[dict[str, Any]] = []
        files = [search_root] if search_root.is_file() else sorted(search_root.rglob("*"))
        for file in files:
            if not file.is_file():
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # skip binary / unreadable files silently (bounded search)
            rel = str(file.relative_to(root))
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append({"path": rel, "line": lineno, "text": line[:1000]})
                    if len(matches) >= _MAX_GREP_MATCHES:
                        return ExecutionResult(
                            success=True,
                            data={"matches": matches, "truncated": True},
                        )
        return ExecutionResult(success=True, data={"matches": matches, "truncated": False})


class GlobConnector(InternalTool):
    """``Glob`` — list sandbox paths matching a glob. Returns ``{paths: [...]}`` (relative)."""

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        pattern = input_data.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return _bad("'pattern' is required")
        root = sandbox_root(context.organisation_id, context.working_dir)
        # glob is rooted at the sandbox; an absolute/`..` pattern can't escape — Path.glob never
        # leaves the base, and we additionally drop anything that resolves outside (symlink safety).
        paths: list[str] = []
        root_resolved = root.resolve()
        for match in sorted(root.glob(pattern.lstrip("/\\"))):
            resolved = match.resolve()
            if resolved != root_resolved and root_resolved not in resolved.parents:
                continue  # a symlinked match pointing outside the sandbox — drop it
            paths.append(str(match.relative_to(root)))
            if len(paths) >= _MAX_GLOB_RESULTS:
                return ExecutionResult(success=True, data={"paths": paths, "truncated": True})
        return ExecutionResult(success=True, data={"paths": paths, "truncated": False})


class BashConnector(InternalTool):
    """``Bash`` — run a command in the org sandbox as a guarded subprocess (capped, timed, no host
    secrets). Output is capped; the command cwd is the sandbox root, so it cannot read host files
    via relative paths. An absolute path inside the command is the agent's own risk surface, bounded
    by the container — full namespace isolation is a follow-up (same posture as script-ingest).
    """

    timeout_s: float = _BASH_OUTER_TIMEOUT_S

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        command = input_data.get("command")
        if not isinstance(command, str) or not command.strip():
            return _bad("'command' is required")
        cwd = sandbox_root(context.organisation_id)
        preexec = set_limits if sys.platform != "win32" else None
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=minimal_env(),  # a clean env — the registry's own secrets never reach the child
                preexec_fn=preexec,
                start_new_session=True,
            )
        except OSError:
            return _bad("the command could not be started", error_type="EXEC_FAILED")
        try:
            stdout, stderr, over = await asyncio.wait_for(
                capped_capture(proc, _BASH_MAX_OUTPUT_BYTES), _BASH_TIMEOUT_S
            )
        except TimeoutError:
            killpg(proc)
            return _bad("the command exceeded its time budget", error_type="TIMEOUT")
        if over:
            killpg(proc)
            return _bad("the command produced too much output", error_type="OUTPUT_TOO_LARGE")
        # stdout/stderr are returned to the agent as tool output; they are the agent's OWN sandboxed
        # command's output (never the registry's). Decode lossily so binary bytes can't crash us.
        return ExecutionResult(
            success=proc.returncode == 0,
            data={
                "exit_code": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            },
            error_type=None if proc.returncode == 0 else "COMMAND_FAILED",
            error_message=None if proc.returncode == 0 else "the command exited non-zero",
        )


class WebSearchConnector(WebResearchConnector):
    """``WebSearch`` — the standard search tool. Delegates to the web-research ``search`` path (same
    BYOM key, same provider factory) so live-web search is shared, not reimplemented. Params
    ``{query, max_results?}`` are forwarded; the operation is forced to ``search``."""

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        forwarded = {**input_data, "operation": "search"}
        return await super()._execute_internal(forwarded, context)


class WebFetchConnector(WebResearchConnector):
    """``WebFetch`` — the standard fetch tool. Delegates to the web-research ``fetch`` path (same
    SSRF-guarded egress gate + redirect re-validation) so URL fetching is shared, not reimplemented.
    Param ``{url}`` is forwarded; the operation is forced to ``fetch`` (keyless)."""

    async def _execute_internal(
        self, input_data: dict[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        forwarded = {**input_data, "operation": "fetch"}
        return await super()._execute_internal(forwarded, context)
