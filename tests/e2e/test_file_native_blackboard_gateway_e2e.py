"""File-native blackboard DEPLOYED-STACK proof through the API GATEWAY — NO fakes (#512, E6).

ADR-040 / lock item 8: a file-native team reads/writes its real git-markdown tree IN PLACE. This
drives the deployed stack (gateway :8006, real capability-registry, real sandbox) and proves that a
file tool bound to a declared ``working_dir`` writes ``bible/*.md`` INTO that tree — not into the
default per-org scratch root — and that the fail-closed confinement guard still rejects an escape.
Real services, real filesystem, nothing mocked, no internal port, no DB-direct.

RED until #512 [impl] threads ``working_dir`` (from the team run's ``workspace_root``, here supplied
directly as the tool instance's configuration) into the file tools' ExecutionContext.

Auto-skips when the gateway is down (conftest); a skip is not a pass.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


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
    """A Write bound to a working tree lands bible/canon.md IN it, not the default scratch."""
    c = gateway_client(register("File-Native")["token"])
    work_tree = f"/tmp/book-ws-{uuid.uuid4().hex}"  # noqa: S108 — declared file-native working tree

    writer = _instance(c, "Write", {"working_dir": work_tree})
    w = _exec(
        c, writer, {"operation": "write", "path": "bible/canon.md", "content": "Alice leads."}
    )
    assert w["status"] == "SUCCESS", w

    # Read bound to the SAME working tree sees it in place
    reader = _instance(c, "Read", {"working_dir": work_tree})
    r = _exec(c, reader, {"operation": "read", "path": "bible/canon.md"})
    assert r["status"] == "SUCCESS" and "Alice leads." in r["output_data"]["content"], r

    # Discriminator: a Read on the DEFAULT scratch root (no working_dir) must NOT find it —
    # proving the write landed in the real tree, not a copy in the per-org sandbox.
    default_reader = _instance(c, "Read", {})
    rd = _exec(c, default_reader, {"operation": "read", "path": "bible/canon.md"})
    assert rd["status"] != "SUCCESS" or "Alice leads." not in str(rd.get("output_data")), rd


def test_a_write_outside_the_working_tree_fails_closed_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """Confinement preserved on the deployed stack: a traversal escape is rejected."""
    c = gateway_client(register("File-Native Guard")["token"])
    work_tree = f"/tmp/book-ws-{uuid.uuid4().hex}"  # noqa: S108 — declared file-native working tree
    writer = _instance(c, "Write", {"working_dir": work_tree})
    out = _exec(c, writer, {"operation": "write", "path": "../escape.md", "content": "nope"})
    assert out["status"] != "SUCCESS", out
