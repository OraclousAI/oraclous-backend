"""Unit: the standard file tools write/read IN PLACE in the team's working tree (#512, E6).

When the execution context carries a ``working_dir`` (the file-native team's real git-markdown tree,
under the org-scoped workspaces root), ``Write``/``Read``/``Edit`` operate against THAT tree — not
the default per-org scratch root. Item 8: the book studio edits its own ``bible/`` in place; nothing
is copied into a store. The untrusted ``working_dir`` is org-confined (a system / cross-org /
outside path fails closed), and the in-tree ``..`` guard is unchanged.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from oraclous_capability_registry_service.domain import sandbox as sb
from oraclous_capability_registry_service.domain.connectors.standard_tools import (
    ReadFileConnector,
    WriteFileConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext

pytestmark = pytest.mark.unit


@pytest.fixture
def workspaces(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(sb, "WORKSPACES_ROOT", root)
    return root


def _ctx(org: uuid.UUID, working_dir: str | None = None) -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=org,
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        working_dir=working_dir,
    )


def _org_tree(workspaces: Path, org: uuid.UUID) -> Path:
    tree = workspaces / str(org) / "book"
    tree.mkdir(parents=True)
    return tree


async def test_write_lands_in_the_declared_working_tree_in_place(workspaces: Path) -> None:
    """A Write with a working tree lands the file IN that tree, not in a copy/scratch dir."""
    org = uuid.uuid4()
    tree = _org_tree(workspaces, org)
    ctx = _ctx(org, working_dir=str(tree))

    res = await WriteFileConnector({}).execute(
        {"path": "bible/canon.md", "content": "Alice is the protagonist."}, ctx
    )
    assert res.success, res.error_message
    assert (tree / "bible" / "canon.md").read_text() == "Alice is the protagonist."


async def test_write_does_not_touch_the_default_scratch_root(workspaces: Path) -> None:
    """The discriminator: with a working tree, nothing is written to the default per-org scratch."""
    org = uuid.uuid4()
    tree = _org_tree(workspaces, org)
    ctx = _ctx(org, working_dir=str(tree))

    await WriteFileConnector({}).execute({"path": "bible/canon.md", "content": "x"}, ctx)

    assert not (sb.sandbox_root(org) / "bible" / "canon.md").exists()


async def test_read_sees_what_write_put_in_the_tree(workspaces: Path) -> None:
    org = uuid.uuid4()
    tree = _org_tree(workspaces, org)
    ctx = _ctx(org, working_dir=str(tree))

    await WriteFileConnector({}).execute({"path": "drafts/ch1.md", "content": "draft one"}, ctx)
    read = await ReadFileConnector({}).execute({"path": "drafts/ch1.md"}, ctx)
    assert read.success and read.data["content"] == "draft one"


async def test_write_outside_the_working_tree_fails_closed(workspaces: Path) -> None:
    """Confinement preserved: a traversal escape within the tree is rejected."""
    org = uuid.uuid4()
    tree = _org_tree(workspaces, org)
    ctx = _ctx(org, working_dir=str(tree))

    res = await WriteFileConnector({}).execute({"path": "../escape.md", "content": "nope"}, ctx)
    assert not res.success
    assert not (tree.parent / "escape.md").exists()


async def test_a_system_working_dir_fails_closed(workspaces: Path) -> None:
    """An untrusted working_dir of ``/`` (secret-exfil vector) is rejected — the write fails."""
    ctx = _ctx(uuid.uuid4(), working_dir="/")
    res = await WriteFileConnector({}).execute({"path": "proc/self/environ", "content": "x"}, ctx)
    assert not res.success
