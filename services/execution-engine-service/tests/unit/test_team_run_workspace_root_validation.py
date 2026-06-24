"""Fail-fast org-scoped validation of a team run's ``workspace_root`` (#518 review note).

The trusted per-run ``workspace_root`` is validated at create (a clear 422), not left to fail at
run time when a member's Write errors. It mirrors the capability-registry guard (#517): the tree
MUST resolve under the org's workspaces root (``WORKSPACES_ROOT/<org>``); a system path, a path
outside the root, the bare root, or ANOTHER org's subtree is rejected. The org segment is the
authenticated org, never user input — so a tenant cannot target another tenant's tree.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from oraclous_execution_engine_service.services import team_run_service as svc
from oraclous_execution_engine_service.services.team_run_service import (
    TeamRunError,
    _validate_workspace_root,
)

pytestmark = pytest.mark.unit

_ORG = uuid.UUID("87654321-4321-8765-4321-876543210000")


@pytest.fixture
def workspaces(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    root.mkdir()
    monkeypatch.setattr(svc, "_WORKSPACES_ROOT", root)
    return root


def test_a_tree_under_the_org_workspaces_root_is_accepted(workspaces: Path) -> None:
    tree = workspaces / str(_ORG) / "book"
    tree.mkdir(parents=True)
    _validate_workspace_root(_ORG, str(tree))  # no raise


@pytest.mark.parametrize("evil", ["/", "/etc", "/etc/passwd", "/proc/self", "/proc/self/environ"])
def test_a_system_path_is_rejected(workspaces: Path, evil: str) -> None:
    with pytest.raises(TeamRunError) as ei:
        _validate_workspace_root(_ORG, evil)
    assert ei.value.status_code == 422 and ei.value.error_type == "invalid_workspace_root"


def test_another_orgs_subtree_is_rejected(workspaces: Path) -> None:
    other = workspaces / str(uuid.uuid4()) / "book"
    other.mkdir(parents=True)
    with pytest.raises(TeamRunError):
        _validate_workspace_root(_ORG, str(other))


def test_the_bare_workspaces_root_without_the_org_segment_is_rejected(workspaces: Path) -> None:
    with pytest.raises(TeamRunError):
        _validate_workspace_root(_ORG, str(workspaces))


def test_a_path_outside_the_workspaces_root_is_rejected(workspaces: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / str(_ORG)
    outside.mkdir(parents=True)
    with pytest.raises(TeamRunError):
        _validate_workspace_root(_ORG, str(outside))
