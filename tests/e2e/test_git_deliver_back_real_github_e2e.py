"""Real-github.com deliver-back clean-delta proof (#542 / #515a — the canonical O7, Reza-signed).

The HUMAN-GATED O7 proof: the already-merged ``core/github-sink`` (#546) delivers into a REAL
github.com repo via a real write PAT — **CONFIGURED THROUGH THE GATEWAY's public credential API**
(stored KMS-sealed by the broker, resolved at execution; NEVER injected into the connector),
exactly as a user would and on the same rails BYOM proves. A recurring refresh writes a clean diff,
NOT a clobber: V1 → a real branch/commit/PR; an identical re-deliver → NO_OP (zero forge writes); a
changed file → only that diff commits to the existing branch/PR.

``github``-marked → DESELECTED in CI (like ``byom``); run LOCALLY (``scripts/e2e.sh --github``) with
``deploy/.env`` creds (``GITHUB_DELIVER_PAT`` + ``GITHUB_DELIVER_REPO``, Reza-provided, never
committed) + **Reza sign-off**. This proof is what CLOSES #515.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.github, pytest.mark.e2e]

_PAT = os.environ.get("GITHUB_DELIVER_PAT")
_REPO = os.environ.get("GITHUB_DELIVER_REPO")  # owner/repo
_GH_API = os.environ.get("GITHUB_API_BASE", "https://api.github.com")
requires_github = pytest.mark.skipif(
    not (_PAT and _REPO),
    reason="GITHUB_DELIVER_PAT/GITHUB_DELIVER_REPO unset (real-github O7 proof; local deploy/.env)",
)


def _gh() -> httpx.Client:
    """A direct github.com client (the PAT) used ONLY to set up + assert — never to deliver (the
    sink delivers via the broker-resolved credential through the gateway)."""
    return httpx.Client(
        base_url=_GH_API,
        headers={"Authorization": f"token {_PAT}", "Accept": "application/vnd.github+json"},
        timeout=30.0,
    )


def _default_branch(g: httpx.Client) -> str:
    r = g.get(f"/repos/{_REPO}")
    assert r.status_code == 200, f"repo {_REPO} not reachable with the PAT: {r.status_code}"
    return r.json().get("default_branch", "main")


def _file_on_branch(g: httpx.Client, path: str, branch: str) -> str | None:
    r = g.get(f"/repos/{_REPO}/contents/{path}", params={"ref": branch})
    if r.status_code != 200:
        return None
    return base64.b64decode(r.json().get("content", "")).decode("utf-8", "replace")


def _cleanup(g: httpx.Client, head: str, pr_number: str | None) -> None:
    """Keep the fixed test repo tidy: close the PR + delete the head branch (best-effort)."""
    if pr_number:
        g.patch(f"/repos/{_REPO}/pulls/{pr_number}", json={"state": "closed"})
    g.request("DELETE", f"/repos/{_REPO}/git/refs/heads/{head}")


def _store_github_pat(c: httpx.Client, user: dict, secret: str) -> str:
    """Store the PAT through the gateway's public credential API — KMS-sealed, never echoed."""
    store = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user["user_id"],
            "name": "github deliver pat",
            "provider": "github",
            "cred_type": "api_key",
            "credential": {"api_key": secret},
        },
    )
    assert store.status_code == 201, store.text
    assert secret not in store.text  # configured, not passed: the secret is sealed, never echoed
    cred_id = store.json()["id"]
    # metadata reads back, but the sealed secret is still never returned
    meta = c.get(f"/credentials/{cred_id}")
    assert meta.status_code == 200, meta.text
    assert secret not in meta.text
    return cred_id


def _instance(c: httpx.Client, cred_id: str) -> str:
    """A ``core/github-sink`` instance BOUND to the repo in its configuration (configured, not
    passed), with the broker credential mapped to it."""
    caps = {x["name"]: x for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    inst = c.post(
        "/api/v1/instances",
        json={
            "capability_id": caps["GitHub Sink"]["id"],
            "name": f"gh-sink-{uuid.uuid4().hex[:8]}",
            "configuration": {"forge": "github", "repo": _REPO},
        },
    )
    assert inst.status_code in (200, 201), inst.text
    instance_id = inst.json()["id"]
    bind = c.post(
        f"/api/v1/instances/{instance_id}/configure-credentials",
        json={"credential_mappings": {"api_key": cred_id}},
    )
    assert bind.status_code in (200, 201), bind.text
    return instance_id


def _deliver(c: httpx.Client, instance_id: str, base: str, head: str, files: list[dict]) -> dict:
    # repo is NOT passed here — it is bound on the instance configuration (configured, not passed)
    r = c.post(
        f"/api/v1/instances/{instance_id}/execute",
        json={
            "input_data": {
                "operation": "deliver",
                "base_branch": base,
                "head_branch": head,
                "files": files,
                "commit_message": "deliver (O7 keyed proof)",
                "pr_title": "O7 deliver-back",
                "pr_body": "automated — #542 real-github proof",
            }
        },
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body.get("status") == "SUCCESS", body
    return body.get("output_data") or {}


@requires_github
def test_real_github_recurring_refresh_writes_a_clean_delta_not_a_clobber(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"ghdeliver{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    # the PAT rides in through the gateway's public credential API — sealed, never injected
    cred_id = _store_github_pat(c, user, str(_PAT))
    instance_id = _instance(c, cred_id)

    g = _gh()
    base = _default_branch(g)
    head = f"deliver/o7-{uuid.uuid4().hex[:8]}"  # unique per run on the fixed repo (no clobber)
    nonce = uuid.uuid4().hex[:8]
    path = f"o7/canon-{nonce}.md"
    pr_number: str | None = None
    try:
        # (1) first deliver → a REAL branch/commit/PR + the file lands on github.com
        out = _deliver(c, instance_id, base, head, [{"path": path, "content": f"V1 {nonce}"}])
        assert out["status"] == "DELIVERED", out
        assert out["changed_paths"] == [path]
        assert out.get("pr_url"), out
        pr_number = out["pr_url"].rstrip("/").split("/")[-1]
        assert g.get(f"/repos/{_REPO}/pulls/{pr_number}").status_code == 200  # a real PR exists
        assert _file_on_branch(g, path, head) == f"V1 {nonce}"

        # (2) re-deliver IDENTICAL → NO_OP (the clean-delta proof; zero forge writes, no clobber)
        out2 = _deliver(c, instance_id, base, head, [{"path": path, "content": f"V1 {nonce}"}])
        assert out2["status"] == "NO_OP", out2
        assert out2["changed_paths"] == []

        # (3) change one file → only that diff commits to the existing branch/PR (the clean delta)
        out3 = _deliver(c, instance_id, base, head, [{"path": path, "content": f"V2 {nonce}"}])
        assert out3["status"] == "DELIVERED", out3
        assert out3["changed_paths"] == [path]
        assert _file_on_branch(g, path, head) == f"V2 {nonce}"
    finally:
        _cleanup(g, head, pr_number)
        g.close()
