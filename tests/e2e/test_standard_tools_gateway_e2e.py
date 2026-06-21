"""Standard agent toolset DEPLOYED-STACK proof through the API GATEWAY — NO fakes (#507).

The standard Claude-Code tools an imported `.claude/agents` team binds (Read/Write/Grep/Glob/Edit/
Bash/WebFetch) are now curated `core/<slug>@1` capabilities. This proves they RESOLVE + DISPATCH +
do real work through the gateway (:8006): a Write→Read round-trip in the per-org sandbox, Glob/Grep
find it, Edit mutates it, Bash runs a guarded subprocess in it, WebFetch fetches a real URL. Real
capability-registry, real sandbox, real subprocess — nothing mocked, no internal port, no DB-direct.

Auto-skips when the gateway is down (conftest); a skip is not a pass.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _cap(c: httpx.Client, name: str) -> dict:
    caps = {x["name"]: x for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    assert name in caps, f"{name} not seeded; got {sorted(caps)}"
    return caps[name]


def _instance(c: httpx.Client, name: str) -> str:
    cap = _cap(c, name)
    inst = c.post(
        "/api/v1/instances",
        json={"capability_id": cap["id"], "name": f"tool-{name.lower()}", "configuration": {}},
    )
    assert inst.status_code == 201, inst.text
    return inst.json()["id"]


def _run(c: httpx.Client, instance_id: str, input_data: dict) -> dict:
    ex = c.post(f"/api/v1/instances/{instance_id}/execute", json={"input_data": input_data})
    assert ex.status_code == 201, ex.text
    out = ex.json()
    assert out["status"] == "SUCCESS", out
    return out["output_data"]


def test_standard_tools_resolve_and_dispatch_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The agent toolset works end-to-end on the deployed stack, through the gateway."""
    user = register("Std Tools")
    c = gateway_client(user["token"])
    path = "chapter1/notes.md"

    # Write -> Read round-trip in the per-org sandbox
    w = _run(
        c, _instance(c, "Write"), {"operation": "write", "path": path, "content": "alpha beta"}
    )
    assert w.get("ok") is True, w
    r = _run(c, _instance(c, "Read"), {"operation": "read", "path": path})
    assert r["content"] == "alpha beta", r

    # Glob finds the written file; Grep matches its content
    g = _run(c, _instance(c, "Glob"), {"operation": "glob", "pattern": "**/*.md"})
    assert any("notes.md" in str(p) for p in (g.get("paths") or g.get("matches") or [])), g
    gr = _run(c, _instance(c, "Grep"), {"operation": "grep", "pattern": "beta"})
    assert gr.get("matches"), gr

    # Edit mutates the file; the change is visible on a re-Read
    _run(
        c,
        _instance(c, "Edit"),
        {"operation": "edit", "path": path, "old_string": "alpha", "new_string": "gamma"},
    )
    r2 = _run(c, _instance(c, "Read"), {"operation": "read", "path": path})
    assert "gamma" in r2["content"] and "alpha" not in r2["content"], r2

    # Bash runs a guarded subprocess in the sandbox
    b = _run(c, _instance(c, "Bash"), {"operation": "bash", "command": "echo sandboxed-ok"})
    assert "sandboxed-ok" in str(b.get("stdout") or b.get("output") or b), b


def test_sandbox_is_org_isolated(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """A second org cannot read the first org's sandbox file (per-org confinement)."""
    a = gateway_client(register("Sandbox A")["token"])
    _run(
        c=a,
        instance_id=_instance(a, "Write"),
        input_data={"operation": "write", "path": "secret.txt", "content": "org-a-only"},
    )

    b = gateway_client(register("Sandbox B")["token"])
    rb = b.post(
        f"/api/v1/instances/{_instance(b, 'Read')}/execute",
        json={"input_data": {"operation": "read", "path": "secret.txt"}},
    )
    # B's sandbox has no such file → the read does not return org-a's content
    assert "org-a-only" not in rb.text


def test_webfetch_keyless_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """WebFetch (keyless, SSRF-guarded) fetches a real URL through the gateway."""
    c = gateway_client(register("WebFetch")["token"])
    out = _run(c, _instance(c, "WebFetch"), {"operation": "fetch", "url": "https://example.com"})
    assert "Example Domain" in str(out.get("content") or out.get("text") or out), out
