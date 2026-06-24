"""Unit: the file-native blackboard reshape of the sandbox (#512, E6 / ADR-040).

The standard file tools confine every path under a per-org scratch root today
(``/tmp/oraclous-agent-sandbox/<org>``). For a file-native team (the book studio's git-markdown
``bible/``/``rules/``/``drafts/``/``production/`` tree) the SAME confinement guard must instead
point at the team's REAL working tree — read/write IN PLACE, no migration into a store — while the
``..``-traversal / escaping-symlink fail-closed guard stays exactly as strong.

These assert the contract of the ``working_dir`` reshape:
  * ``sandbox_root(org, working_dir=X)`` returns X (the real tree), not the default scratch root;
  * ``resolve_in_sandbox(org, rel, working_dir=X)`` confines under X;
  * the traversal guard still fails closed under a working tree (``..`` / absolute / symlink-out);
  * with no ``working_dir`` the legacy per-org scratch behaviour is unchanged (back-compat).

RED until #512 [impl] adds the ``working_dir`` parameter.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
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


def test_sandbox_root_uses_the_declared_working_tree(org: uuid.UUID, tmp_path: Path) -> None:
    """With a declared working tree, the root IS that tree — not the default scratch root."""
    tree = tmp_path / "book"
    tree.mkdir()
    root = sandbox_root(org, working_dir=str(tree))
    assert root.resolve() == tree.resolve()
    # and it is NOT the default /tmp/oraclous-agent-sandbox/<org> scratch root
    assert SANDBOX_PARENT not in root.resolve().parents


def test_sandbox_root_without_working_dir_is_the_legacy_per_org_scratch(org: uuid.UUID) -> None:
    """Back-compat: no working_dir → the unchanged per-org scratch root under SANDBOX_PARENT."""
    root = sandbox_root(org)
    assert root == SANDBOX_PARENT / str(org)


def test_resolve_confines_a_relative_path_under_the_working_tree(
    org: uuid.UUID, tmp_path: Path
) -> None:
    """A member writing ``bible/canon.md`` resolves IN the real tree, in place."""
    tree = tmp_path / "book"
    tree.mkdir()
    resolved = resolve_in_sandbox(org, "bible/canon.md", working_dir=str(tree))
    assert resolved == (tree / "bible" / "canon.md").resolve()


@pytest.mark.parametrize("escape", ["../escape.md", "../../etc/passwd", "bible/../../escape.md"])
def test_traversal_still_fails_closed_under_a_working_tree(
    org: uuid.UUID, tmp_path: Path, escape: str
) -> None:
    """The fail-closed guard is unchanged under a working tree: no path may escape it."""
    tree = tmp_path / "book"
    tree.mkdir()
    with pytest.raises(SandboxPathError):
        resolve_in_sandbox(org, escape, working_dir=str(tree))


def test_escaping_symlink_under_a_working_tree_fails_closed(org: uuid.UUID, tmp_path: Path) -> None:
    """A symlink inside the tree that points OUT of it is rejected before any filesystem op."""
    tree = tmp_path / "book"
    tree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tree / "link").symlink_to(outside)
    with pytest.raises(SandboxPathError):
        resolve_in_sandbox(org, "link/secret.md", working_dir=str(tree))
