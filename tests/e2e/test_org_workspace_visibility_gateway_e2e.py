"""Workspace visibility END-TO-END through the API GATEWAY — #736 / ADR-051. No fakes.

Three real users on the deployed stack: an owner who builds the company knowledge base, a colleague
who is really invited and really joins that organisation, and an outsider who never is. Everything
goes through the gateway on :8006 over the public HTTP API — registration, the invitation, the org
switch, the ingest, the reads and the refused writes. Nothing is injected server-side and nothing is
asserted against the database.

It proves both halves of the decision at once:

* the invited member SEES and READS the workspace they do not own (ADR-051 decision 2), and
* the member still cannot rename, delete or ingest into it, while the outsider sees nothing at all
  on any route (decision 3, and the control the widening must not remove).

Residual risk this does NOT fix, recorded in ADR-051 decision 4: there is no per-workspace member
set, so every graph in an organisation is readable by every member of it. Tracked as #737.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _new_graph(c: httpx.Client, name: str) -> str:
    g = c.post("/api/v1/graphs", json={"name": name})
    assert g.status_code == 201, g.text
    return str(g.json()["id"])


def _ingest(c: httpx.Client, graph_id: str, text: str) -> str:
    """Ingest one document and wait for the async job to reach a terminal state."""
    r = c.post(f"/api/v1/graphs/{graph_id}/ingest", json={"content": text, "source_type": "text"})
    assert r.status_code == 202, f"ingest failed: {r.status_code} {r.text}"
    job_id = str(r.json()["id"])
    for _ in range(90):
        status = str(c.get(f"/api/v1/graphs/{graph_id}/jobs/{job_id}").json().get("status")).upper()
        if status in ("SUCCEEDED", "COMPLETED"):
            return job_id
        if status in ("FAILED", "ERROR"):
            raise AssertionError(f"ingest job {job_id} failed: {status}")
        time.sleep(2)
    raise AssertionError(f"ingest job {job_id} never completed")


def _join_org(
    owner: dict,
    joiner: dict,
    gateway_client: Callable[[str], httpx.Client],
    gateway_url: str,
) -> httpx.Client:
    """The real invitation journey: invite → peek → accept → switch the session to the joined org.

    `switch-org` is what binds the joiner's next requests to the owner's organisation; the org id
    travels in the header, never a body field."""
    o = gateway_client(owner["token"])
    invite = o.post(
        f"/v1/orgs/{owner['org_id']}/invitations",
        json={"email": joiner["email"], "org_role": "member"},
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["token"]

    j = gateway_client(joiner["token"])
    peek = j.post("/v1/invitations/peek", json={"token": token})
    assert peek.status_code == 200, peek.text
    assert peek.json()["organisation_id"] == owner["org_id"]
    assert j.post("/v1/invitations/accept", json={"token": token}).status_code == 200

    switched = httpx.post(
        f"{gateway_url}/v1/auth/switch-org",
        headers={
            "Authorization": f"Bearer {joiner['token']}",
            "X-Organisation-Id": owner["org_id"],
        },
        timeout=30.0,
    )
    assert switched.status_code == 200, switched.text
    member = gateway_client(switched.json()["access_token"])
    assert member.get("/v1/auth/me").json()["organisation_id"] == owner["org_id"]
    return member


def test_an_invited_member_finds_and_reads_the_organisations_workspace(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
    gateway_url: str,
) -> None:
    owner = register(f"kbowner{uuid.uuid4().hex[:8]} user")
    colleague = register(f"kbmember{uuid.uuid4().hex[:8]} user")
    outsider = register(f"kbrival{uuid.uuid4().hex[:8]} user")

    o = gateway_client(owner["token"])
    name = f"Company Knowledge Base {uuid.uuid4().hex[:6]}"
    graph_id = _new_graph(o, name)
    _ingest(o, graph_id, "The expense policy allows travel booked fourteen days in advance.")

    member = _join_org(owner, colleague, gateway_client, gateway_url)

    # ── acceptance 1: the member finds the workspace, and can tell it is not theirs ────────────
    listed = member.get("/api/v1/graphs")
    assert listed.status_code == 200, listed.text
    rows = {g["id"]: g for g in listed.json()}
    assert graph_id in rows, f"the member cannot see the org's workspace: {listed.text[:400]}"
    assert rows[graph_id]["name"] == name
    assert rows[graph_id]["is_owner"] is False
    owned = {g["id"]: g for g in o.get("/api/v1/graphs").json()}
    assert owned[graph_id]["is_owner"] is True  # and the owner still sees it as theirs

    # the detail route agrees with the list — no row that 404s when opened
    detail = member.get(f"/api/v1/graphs/{graph_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["is_owner"] is False

    # ── acceptance 2: the member lists its documents ──────────────────────────────────────────
    docs = member.get(f"/api/v1/graphs/{graph_id}/documents")
    assert docs.status_code == 200, docs.text
    assert len(docs.json()) >= 1, "the member sees the workspace but none of its documents"

    arts = member.get(f"/v1/artifacts?graph_id={graph_id}")
    assert arts.status_code == 200, arts.text

    # ── acceptance 3: read did not become write ───────────────────────────────────────────────
    assert member.patch(f"/api/v1/graphs/{graph_id}", json={"name": "hijacked"}).status_code == 404
    assert member.delete(f"/api/v1/graphs/{graph_id}").status_code == 404
    assert (
        member.post(
            f"/api/v1/graphs/{graph_id}/ingest",
            json={"content": "smuggled", "source_type": "text"},
        ).status_code
        == 404
    )
    assert o.get(f"/api/v1/graphs/{graph_id}").json()["name"] == name  # untouched

    # ── acceptance 4: another organisation sees none of it, on every route ────────────────────
    out = gateway_client(outsider["token"])
    assert graph_id not in {g["id"] for g in out.get("/api/v1/graphs").json()}
    assert out.get(f"/api/v1/graphs/{graph_id}").status_code == 404
    assert out.get(f"/api/v1/graphs/{graph_id}/documents").status_code == 404
    assert out.get(f"/v1/artifacts?graph_id={graph_id}").status_code == 404
    assert out.get(f"/api/v1/graphs/{graph_id}/ontology").status_code == 404
    assert out.get(f"/api/v1/graphs/{graph_id}/analytics").status_code == 404
    assert out.patch(f"/api/v1/graphs/{graph_id}", json={"name": "hijacked"}).status_code == 404
    # and no content leaks through the search path either
    hijack = out.post("/v1/search/hybrid", json={"query": "expense", "graph_id": graph_id})
    assert hijack.status_code != 200 or hijack.json() == []

    # ── acceptance 5: federated search fans out over EXACTLY the listed set ───────────────────
    fed = member.post("/v1/federated/search", json={"query": "expense policy", "mode": "fulltext"})
    assert fed.status_code == 200, fed.text
    queried = {g["id"] for g in fed.json()["meta"]["graphs_queried"]}
    visible = {g["id"] for g in member.get("/api/v1/graphs").json()}
    assert queried == visible, (
        "the console list and the ADR-026 federation set disagree — "
        f"listed {sorted(visible)}, queried {sorted(queried)}"
    )
