"""Deliver-back clean-delta e2e through the GATEWAY against a REAL Gitea forge (#515, E6 / O7).

The canonical CI-deterministic O7 proof: a team's output lands in the user's git tree (a head branch
+ a PR) via the `core/github-sink` capability, and a **recurring refresh writes a clean diff, NOT a
clobber** — re-delivering identical content is a NO_OP; a changed file writes only that diff. The
forge is a REAL Gitea container (real commits/branches/PRs over the Contents API) — no fakes, no
stub, the same real-substrate pattern as the testcontainers. Oraclous's path is 100% real
(connector → broker PAT → Gitea Contents API → PR), nothing of ours mocked.

No fakes: real registration → real gateway → real capability instance execution → real Gitea. The
real-github.com proof is #542. Auto-skips when the gateway or the Gitea env is absent (a skip is NOT
a pass — the deployed-stack run provides both).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

# the [impl]/compose provides a real Gitea: GITEA_API_BASE (host-reachable, for test setup),
# GITEA_INTERNAL_BASE (compose-network, bound on the sink instance for the registry container), + a
# GITEA_TOKEN (a real PAT used both to set up the repo and as the deliver credential).
_GITEA_API = os.environ.get("GITEA_API_BASE")
_GITEA_INTERNAL = os.environ.get("GITEA_INTERNAL_BASE") or _GITEA_API
_GITEA_TOKEN = os.environ.get("GITEA_TOKEN")
requires_gitea = pytest.mark.skipif(
    not (_GITEA_API and _GITEA_TOKEN), reason="GITEA_API_BASE/GITEA_TOKEN not set (real-forge e2e)"
)


def _gitea() -> httpx.Client:
    return httpx.Client(
        base_url=str(_GITEA_API).rstrip("/"),
        headers={"Authorization": f"token {_GITEA_TOKEN}"},
        timeout=30.0,
    )


def _fresh_repo(g: httpx.Client) -> str:
    """Create a fresh auto-initialised repo (a `main` branch with a README) → '<owner>/<name>'."""
    me = g.get("/user").json()
    name = f"deliver-{uuid.uuid4().hex[:8]}"
    r = g.post("/user/repos", json={"name": name, "auto_init": True, "private": True})
    assert r.status_code in (200, 201), r.text
    return f"{me['login']}/{name}"


def _file_on_branch(g: httpx.Client, repo: str, path: str, branch: str) -> str | None:
    """The decoded content of a file on a branch in Gitea, else None (the real-landing check)."""
    import base64

    r = g.get(f"/repos/{repo}/contents/{path}", params={"ref": branch})
    if r.status_code != 200:
        return None
    return base64.b64decode(r.json().get("content", "")).decode("utf-8", "replace")


def _instance(c: httpx.Client, user: dict, repo: str) -> str:
    """A `core/github-sink` instance bound to the Gitea forge + the user's PAT (via the broker)."""
    cred = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "gitea pat",
            "provider": "github",
            "cred_type": "api_key",
            "credential": {"api_key": _GITEA_TOKEN},
        },
    )
    assert cred.status_code == 201, cred.text
    caps = {x["name"]: x for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    inst = c.post(
        "/api/v1/instances",
        json={
            "capability_id": caps["GitHub Sink"]["id"],
            "name": f"sink-{uuid.uuid4().hex[:8]}",
            "configuration": {"forge": "gitea", "base_url": _GITEA_INTERNAL},
        },
    )
    assert inst.status_code in (200, 201), inst.text
    instance_id = inst.json()["id"]
    c.post(
        f"/api/v1/instances/{instance_id}/configure-credentials",
        json={"credential_mappings": {"api_key": cred.json()["id"]}},
    )
    return instance_id


def _deliver(c: httpx.Client, instance_id: str, repo: str, files: list[dict]) -> dict:
    r = c.post(
        f"/api/v1/instances/{instance_id}/execute",
        json={
            "input_data": {
                "operation": "deliver",
                "repo": repo,
                "base_branch": "main",
                "head_branch": "deliver/book",
                "files": files,
                "commit_message": "deliver",
                "pr_title": "Book delivery",
                "pr_body": "automated",
            }
        },
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body.get("status") == "SUCCESS", body
    return body.get("output_data") or {}


@requires_gitea
def test_a_recurring_refresh_writes_a_clean_delta_not_a_clobber(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"deliver{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    g = _gitea()
    repo = _fresh_repo(g)
    instance_id = _instance(c, user, repo)
    nonce = uuid.uuid4().hex[:8]

    # (1) first deliver → all files written, a PR opened, and they REALLY land in gitea
    out = _deliver(c, instance_id, repo, [{"path": "bible/canon.md", "content": f"V1 {nonce}"}])
    assert out["status"] == "DELIVERED"
    assert out["changed_paths"] == ["bible/canon.md"]
    assert out.get("pr_url")
    assert _file_on_branch(g, repo, "bible/canon.md", "deliver/book") == f"V1 {nonce}"

    # (2) re-deliver IDENTICAL content → NO_OP, nothing written (the clean-delta proof, no clobber)
    out2 = _deliver(c, instance_id, repo, [{"path": "bible/canon.md", "content": f"V1 {nonce}"}])
    assert out2["status"] == "NO_OP"
    assert out2["changed_paths"] == []

    # (3) re-deliver a CHANGED file → only that diff is written (deterministic delta)
    out3 = _deliver(c, instance_id, repo, [{"path": "bible/canon.md", "content": f"V2 {nonce}"}])
    assert out3["status"] == "DELIVERED"
    assert out3["changed_paths"] == ["bible/canon.md"]
    assert _file_on_branch(g, repo, "bible/canon.md", "deliver/book") == f"V2 {nonce}"
