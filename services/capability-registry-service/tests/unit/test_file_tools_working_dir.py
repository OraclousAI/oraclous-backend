"""Unit: the standard file tools write/read IN PLACE in the team's working tree (#512, E6).

When the execution context carries a ``working_dir`` (the file-native team's real git-markdown
tree), ``Write``/``Read``/``Edit`` operate against THAT tree — not the default per-org scratch root.
This is the heart of lock item 8: the book studio edits its own ``bible/`` in place; nothing is
copied into a store. Confinement is unchanged: an escape still fails closed.

RED until #512 [impl] adds ``ExecutionContext.working_dir`` and threads it into the file connectors.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from oraclous_capability_registry_service.domain.connectors.standard_tools import (
    ReadFileConnector,
    WriteFileConnector,
)
from oraclous_capability_registry_service.domain.executors.base import ExecutionContext

pytestmark = pytest.mark.unit


def _ctx(org: uuid.UUID, working_dir: str | None = None) -> ExecutionContext:
    return ExecutionContext(
        instance_id=uuid.uuid4(),
        organisation_id=org,
        user_id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        working_dir=working_dir,
    )


async def test_write_lands_in_the_declared_working_tree_in_place(tmp_path: Path) -> None:
    """A Write with a working tree lands the file IN that tree, not in a copy/scratch dir."""
    org = uuid.uuid4()
    tree = tmp_path / "book"
    tree.mkdir()
    ctx = _ctx(org, working_dir=str(tree))

    res = await WriteFileConnector({}).execute(
        {"path": "bible/canon.md", "content": "Alice is the protagonist."}, ctx
    )
    assert res.success, res.error_message
    # the file is physically present in the REAL working tree
    assert (tree / "bible" / "canon.md").read_text() == "Alice is the protagonist."


async def test_write_does_not_touch_the_default_scratch_root(tmp_path: Path) -> None:
    """The discriminator: with a working tree, nothing is written to the default per-org scratch."""
    from oraclous_capability_registry_service.domain.sandbox import sandbox_root

    org = uuid.uuid4()
    tree = tmp_path / "book"
    tree.mkdir()
    ctx = _ctx(org, working_dir=str(tree))

    await WriteFileConnector({}).execute({"path": "bible/canon.md", "content": "x"}, ctx)

    # the default per-org scratch root must NOT have received the file (proves in-place, not copy)
    assert not (sandbox_root(org) / "bible" / "canon.md").exists()


async def test_read_sees_what_write_put_in_the_tree(tmp_path: Path) -> None:
    org = uuid.uuid4()
    tree = tmp_path / "book"
    tree.mkdir()
    ctx = _ctx(org, working_dir=str(tree))

    await WriteFileConnector({}).execute({"path": "drafts/ch1.md", "content": "draft one"}, ctx)
    read = await ReadFileConnector({}).execute({"path": "drafts/ch1.md"}, ctx)
    assert read.success and read.data["content"] == "draft one"


async def test_write_outside_the_working_tree_fails_closed(tmp_path: Path) -> None:
    """Confinement preserved: a traversal escape is rejected even with a working tree."""
    org = uuid.uuid4()
    tree = tmp_path / "book"
    tree.mkdir()
    ctx = _ctx(org, working_dir=str(tree))

    res = await WriteFileConnector({}).execute({"path": "../escape.md", "content": "nope"}, ctx)
    assert not res.success
    assert not (tmp_path / "escape.md").exists()
