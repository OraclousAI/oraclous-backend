"""Per-organisation file sandbox (domain layer) — the confined workspace for the standard tools.

The standard ``Read``/``Write``/``Edit``/``Grep``/``Glob``/``Bash`` tools an imported ``.claude``
agent team binds give the agent *functional, isolated file semantics within a run*. They are NOT a
window onto the registry host: every path resolves under a single per-organisation scratch root, and
any path that escapes it (``..`` traversal, an absolute path, a symlink that points out) is rejected
fail-closed BEFORE any filesystem op. The root is created lazily on first use.

Scope note: isolation is per-ORGANISATION here, not per-execution — two runs in the same org share
the scratch dir. That is enough to make a single imported team functional and keeps cross-org
isolation absolute (the org-id is in the path). Full per-execution isolation is a tracked follow-up.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

#: parent of every org's scratch dir. Under /tmp so it is host-ephemeral and world-isolated by org;
#: the org-id segment (added per call) is what isolates orgs. Overridable for prod via the env var.
SANDBOX_PARENT = Path(
    os.environ.get("ORACLOUS_AGENT_SANDBOX_ROOT", "/tmp/oraclous-agent-sandbox")  # noqa: S108
)


class SandboxPathError(ValueError):
    """A requested path escapes (or could not be confined to) the organisation sandbox root."""


def sandbox_root(organisation_id: uuid.UUID) -> Path:
    """The org's scratch root, created on first use. The org-id segment keeps orgs isolated."""
    root = SANDBOX_PARENT / str(organisation_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_in_sandbox(organisation_id: uuid.UUID, rel_path: str) -> Path:
    """Resolve ``rel_path`` to an absolute path CONFINED under the org sandbox root, fail-closed.

    A leading ``/`` is treated as sandbox-root-relative (an agent saying ``/notes.txt`` means the
    sandbox's ``notes.txt``, never the host root). After resolution the path MUST sit under the root
    — ``..`` traversal, an escaping symlink, or anything that lands outside raises
    :class:`SandboxPathError`. The path need not exist (a Write target won't yet)."""
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise SandboxPathError("a non-empty 'path' is required")
    root = sandbox_root(organisation_id)
    # Treat an absolute path as root-relative (strip leading separators) so it can never reach the
    # host filesystem root; a relative path joins onto the root as written.
    candidate = (root / rel_path.lstrip("/\\")).resolve()
    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        # Escapes the sandbox (traversal / symlink / odd join) — refuse before any filesystem op.
        raise SandboxPathError("path escapes the sandbox workspace")
    return candidate
