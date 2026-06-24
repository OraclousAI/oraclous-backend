"""File-native blackboard DEPLOYED-STACK proof through the API GATEWAY — NO fakes (#512, E6).

ADR-040 / lock item 8: a file-native team reads/writes its real git-markdown tree IN PLACE. This
drives the deployed stack (gateway :8006, real capability-registry, real sandbox) and proves:
  * a file tool bound to a working tree UNDER the org-scoped workspaces root writes ``bible/*.md``
    INTO that tree (not the default scratch root); and
  * the untrusted ``working_dir`` is confined fail-closed — a system path (``/``) and another org's
    workspace are REJECTED through the gateway (org-scoping / operator-separation, ADR-006/008).
Real services, real filesystem, nothing mocked, no internal port, no DB-direct.

Auto-skips when the gateway is down (conftest); a skip is not a pass.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

# The capability-registry container's default org-scoped workspaces root (sandbox.WORKSPACES_ROOT);
# a real working tree must live under ``<root>/<org>/…``. The operator overrides this in prod.
_WORKSPACES_ROOT = "/tmp/oraclous-agent-workspaces"  # noqa: S108 — container-local default


def _cap_id(c: httpx.Client, name: str) -> str:
    caps = {x["name"]: x for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    assert name in caps, f"{name} not seeded; got {sorted(caps)}"
    return caps[name]["id"]


def _instance(c: httpx.Client, name: str, configuration: dict | None = None) -> str:
    inst = c.post(
        "/api/v1/instances",
        json={
            "capability_id": _cap_id(c, name),
            "name": f"fn-{name.lower()}-{uuid.uuid4().hex[:6]}",
            "configuration": configuration or {},
        },
    )
    assert inst.status_code == 201, inst.text
    return inst.json()["id"]


def _exec(c: httpx.Client, instance_id: str, input_data: dict) -> dict:
    ex = c.post(f"/api/v1/instances/{instance_id}/execute", json={"input_data": input_data})
    assert ex.status_code == 201, ex.text
    return ex.json()


def test_a_file_native_member_writes_bible_md_in_place_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """A Write bound to the org's tree lands bible/canon.md IN it, not the default scratch."""
    user = register("File-Native")
    c = gateway_client(user["token"])
    work_tree = f"{_WORKSPACES_ROOT}/{user['org_id']}/book-{uuid.uuid4().hex[:8]}"

    writer = _instance(c, "Write", {"working_dir": work_tree})
    w = _exec(
        c, writer, {"operation": "write", "path": "bible/canon.md", "content": "Alice leads."}
    )
    assert w["status"] == "SUCCESS", w

    reader = _instance(c, "Read", {"working_dir": work_tree})
    r = _exec(c, reader, {"operation": "read", "path": "bible/canon.md"})
    assert r["status"] == "SUCCESS" and "Alice leads." in r["output_data"]["content"], r

    # Discriminator: a Read on the DEFAULT scratch root (no working_dir) must NOT find it.
    default_reader = _instance(c, "Read", {})
    rd = _exec(c, default_reader, {"operation": "read", "path": "bible/canon.md"})
    assert rd["status"] != "SUCCESS" or "Alice leads." not in str(rd.get("output_data")), rd


def test_an_in_tree_escape_fails_closed_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Within a valid working tree the ``..`` guard still rejects a traversal escape."""
    user = register("File-Native Guard")
    c = gateway_client(user["token"])
    work_tree = f"{_WORKSPACES_ROOT}/{user['org_id']}/book-{uuid.uuid4().hex[:8]}"
    writer = _instance(c, "Write", {"working_dir": work_tree})
    out = _exec(c, writer, {"operation": "write", "path": "../escape.md", "content": "nope"})
    assert out["status"] != "SUCCESS", out


def test_a_system_working_dir_is_rejected_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The flagged escape: ``working_dir="/"`` + read /proc/self/environ is REJECTED."""
    c = gateway_client(register("File-Native SSRF")["token"])
    reader = _instance(c, "Read", {"working_dir": "/"})
    out = _exec(c, reader, {"operation": "read", "path": "proc/self/environ"})
    assert out["status"] != "SUCCESS", out
    assert "OPENROUTER" not in str(out.get("output_data")), "container env must never be readable"


def test_another_orgs_workspace_is_rejected_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """A working_dir under ANOTHER org's subtree is a cross-tenant escape — rejected."""
    user = register("File-Native CrossOrg")
    c = gateway_client(user["token"])
    other_tree = f"{_WORKSPACES_ROOT}/{uuid.uuid4()}/book"  # a different org's path
    reader = _instance(c, "Read", {"working_dir": other_tree})
    out = _exec(c, reader, {"operation": "read", "path": "bible/canon.md"})
    assert out["status"] != "SUCCESS", out
