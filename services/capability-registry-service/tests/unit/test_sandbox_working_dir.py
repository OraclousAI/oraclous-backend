"""Unit: the file-native blackboard reshape of the sandbox (#512, E6 / ADR-040).

The standard file tools confine every path under a per-org scratch root today
(``/tmp/oraclous-agent-sandbox/<org>``). For a file-native team (the book studio's git-markdown
``bible/``/``rules/``/``drafts/``/``production/`` tree) the SAME confinement guard instead points at
the team's REAL working tree — read/write IN PLACE, no migration into a store.

Crucially ``working_dir`` is UNTRUSTED (user-controlled instance config), so it is itself confined
fail-closed under the operator-configured, org-scoped workspaces root (``WORKSPACES_ROOT/<org>``):
a system path, a path outside the root, or ANOTHER org's subtree is rejected before any filesystem
op (ADR-006 org-scoping, ADR-008 operator separation, §11). Within a valid tree the ``..``-traversal
/ escaping-symlink guard stays exactly as strong.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from oraclous_capability_registry_service.domain import sandbox as sb
from oraclous_capability_registry_service.domain.sandbox import (
    SANDBOX_PARENT,
    SandboxPathError,
    resolve_in_sandbox,
    sandbox_root,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def org() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def workspaces(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the org-scoped workspaces root at a tmp dir (the operator-mounted root in prod)."""
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(sb, "WORKSPACES_ROOT", root)
    return root


def _org_tree(workspaces: Path, org: uuid.UUID) -> Path:
    tree = workspaces / str(org) / "book"
    tree.mkdir(parents=True)
    return tree


# --------------------------------------------------------------------------- valid working tree


def test_sandbox_root_uses_the_declared_working_tree(org: uuid.UUID, workspaces: Path) -> None:
    """A working tree under the org's workspaces root IS the root — not the default scratch root."""
    tree = _org_tree(workspaces, org)
    root = sandbox_root(org, working_dir=str(tree))
    assert root.resolve() == tree.resolve()
    assert SANDBOX_PARENT not in root.resolve().parents


def test_sandbox_root_without_working_dir_is_the_legacy_per_org_scratch(org: uuid.UUID) -> None:
    """Back-compat: no working_dir → the unchanged per-org scratch root under SANDBOX_PARENT."""
    root = sandbox_root(org)
    assert root == SANDBOX_PARENT / str(org)


def test_resolve_confines_a_relative_path_under_the_working_tree(
    org: uuid.UUID, workspaces: Path
) -> None:
    """A member writing ``bible/canon.md`` resolves IN the real tree, in place."""
    tree = _org_tree(workspaces, org)
    resolved = resolve_in_sandbox(org, "bible/canon.md", working_dir=str(tree))
    assert resolved == (tree / "bible" / "canon.md").resolve()


@pytest.mark.parametrize("escape", ["../escape.md", "../../etc/passwd", "bible/../../escape.md"])
def test_traversal_still_fails_closed_under_a_working_tree(
    org: uuid.UUID, workspaces: Path, escape: str
) -> None:
    """The fail-closed guard is unchanged within a valid working tree: no path may escape it."""
    tree = _org_tree(workspaces, org)
    with pytest.raises(SandboxPathError):
        resolve_in_sandbox(org, escape, working_dir=str(tree))


def test_escaping_symlink_under_a_working_tree_fails_closed(
    org: uuid.UUID, workspaces: Path, tmp_path: Path
) -> None:
    """A symlink inside the tree that points OUT of it is rejected before any filesystem op."""
    tree = _org_tree(workspaces, org)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tree / "link").symlink_to(outside)
    with pytest.raises(SandboxPathError):
        resolve_in_sandbox(org, "link/secret.md", working_dir=str(tree))


# --------------------------------------------------------------------------- untrusted working_dir
# The working_dir itself is user-controlled — these MUST be rejected, not merely confined-within.


@pytest.mark.parametrize("evil", ["/", "/etc", "/etc/passwd", "/proc/self", "/proc/self/environ"])
def test_a_system_path_working_dir_is_rejected(org: uuid.UUID, workspaces: Path, evil: str) -> None:
    """``working_dir="/"`` (→ read /proc/self/environ = secret exfiltration) fails closed."""
    with pytest.raises(SandboxPathError):
        sandbox_root(org, working_dir=evil)


def test_another_orgs_workspace_is_rejected(org: uuid.UUID, workspaces: Path) -> None:
    """A working_dir under ANOTHER org's subtree is a cross-tenant escape — rejected."""
    other = uuid.uuid4()
    other_tree = workspaces / str(other) / "book"
    other_tree.mkdir(parents=True)
    with pytest.raises(SandboxPathError):
        sandbox_root(org, working_dir=str(other_tree))


def test_the_bare_workspaces_root_without_the_org_segment_is_rejected(
    org: uuid.UUID, workspaces: Path
) -> None:
    """The org segment is mandatory — the shared workspaces root itself is not an allowed tree."""
    with pytest.raises(SandboxPathError):
        sandbox_root(org, working_dir=str(workspaces))


def test_a_path_outside_the_workspaces_root_is_rejected(
    org: uuid.UUID, workspaces: Path, tmp_path: Path
) -> None:
    """Even a sibling of the workspaces root (a plausible-looking dir) is outside → rejected."""
    elsewhere = tmp_path / "elsewhere" / str(org)
    elsewhere.mkdir(parents=True)
    with pytest.raises(SandboxPathError):
        sandbox_root(org, working_dir=str(elsewhere))
