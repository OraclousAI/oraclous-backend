"""A bad team-run ``workspace_root`` is rejected fail-fast at CREATE — DEPLOYED-STACK, gateway, no
fakes (#518 review note).

The trusted per-run ``workspace_root`` is validated org-scoped at create, so a system / cross-org /
outside path is a clear 4xx at ``POST /v1/engine/team-runs`` — not a confusing mid-run member
failure. Deterministic (the validation fires before any harness call), so it runs in the standard
e2e lane. Real gateway, real engine; nothing mocked.

Auto-skips when the gateway is down (conftest); a skip is not a pass.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_WORKSPACES_ROOT = "/tmp/oraclous-agent-workspaces"  # noqa: S108 — container-local default (#517)


def _one_member_team(root: Path) -> None:
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "scribe.md").write_text(
        "---\nname: scribe\nmodel: sonnet\ntools: Write\n---\nwrite.\n"
    )
    (root / "teams" / "1-canon").mkdir(parents=True)
    (root / "teams" / "1-canon" / "charter.md").write_text(
        "# Team\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `scribe` | subagent | sonnet | write |\n"
    )


def _post_run(c: httpx.Client, root: Path, workspace_root: str) -> httpx.Response:
    imported = import_setup(root, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    return c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": imported.manifest.model_dump(mode="json"),
            "sub_harnesses": dict(imported.sub_harnesses),
            "gate_decisions": {},
            "workspace_root": workspace_root,
        },
    )


@pytest.mark.parametrize("evil", ["/", "/etc"])
def test_a_system_workspace_root_is_rejected_at_create_through_the_gateway(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
    evil: str,
) -> None:
    c = gateway_client(register("WS Validate")["token"])
    _one_member_team(tmp_path)
    resp = _post_run(c, tmp_path, evil)
    assert 400 <= resp.status_code < 500, resp.text  # rejected at create, not 202-accepted
    assert "workspace_root" in resp.text.lower() or "validation" in resp.text.lower(), resp.text


def test_another_orgs_workspace_root_is_rejected_at_create_through_the_gateway(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    c = gateway_client(register("WS CrossOrg")["token"])
    _one_member_team(tmp_path)
    other = f"{_WORKSPACES_ROOT}/{uuid.uuid4()}/book"  # a different org's subtree
    resp = _post_run(c, tmp_path, other)
    assert 400 <= resp.status_code < 500, resp.text


def test_a_valid_org_scoped_workspace_root_is_accepted_at_create(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """The happy path: a tree under the org's own workspaces root is accepted (202)."""
    user = register("WS Valid")
    c = gateway_client(user["token"])
    _one_member_team(tmp_path)
    good = f"{_WORKSPACES_ROOT}/{user['org_id']}/book-{uuid.uuid4().hex[:8]}"
    resp = _post_run(c, tmp_path, good)
    assert resp.status_code == 202, resp.text  # validated + queued (the worker drives it)
