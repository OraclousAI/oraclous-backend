"""Per-run graph_id binding — org-scoped fail-fast validation END-TO-END through the GATEWAY.

#524 (E6 / ADR-040 Decision 7). A graph-bound team run names a ``graph_id`` once; the engine
validates it org-scoped at create by GETting the graph from the knowledge-graph-service over the
trusted-gateway downstream identity. A graph the caller's org does NOT own (or that does not exist)
is rejected fail-fast with a 422 at create — never a confusing mid-run member failure — exactly like
#520's ``workspace_root``. The caller's OWN graph is accepted (202, the worker drives it).

No fakes: real registration → real JWT → real gateway → real engine create → real KGS GET. The
graph is created through the gateway (``POST /api/v1/graphs`` → knowledge-graph-service); the run is
created through the gateway (``POST /v1/engine/team-runs``). Deterministic (no LLM) → runs in CI's
deployed-stack e2e (fake harness mode); auto-skips when the gateway is down.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from oraclous_ohm.import_.setup import import_setup

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _uniq(label: str) -> str:
    """A full_name whose FIRST token is unique — the auth-service derives the personal-org name (and
    slug) from it, so a unique first token gives each registration its own org/slug (never piling
    onto a shared, retry-exhaustible slug space across repeated e2e runs)."""
    return f"{label}{uuid.uuid4().hex[:10]} user"


def _one_member_team(root: Path) -> None:
    """A minimal one-member studio — enough to be a valid team manifest; the run's graph_id binding
    is what's under test, not the members' work."""
    agents = root / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "scribe.md").write_text(
        "---\nname: scribe\nmodel: sonnet\ntools: Write\n---\nrecord.\n"
    )
    (root / "teams" / "1-canon").mkdir(parents=True)
    (root / "teams" / "1-canon" / "charter.md").write_text(
        "# Team\n## Roster\n| Agent | Type | Model | Job |\n| --- | --- | --- | --- |\n"
        "| `scribe` | subagent | sonnet | record |\n"
    )


def _post_run(c: httpx.Client, root: Path, graph_id: str) -> httpx.Response:
    imported = import_setup(root, owner_organization_id=uuid.uuid4(), name="studio")
    assert imported.manifest is not None
    return c.post(
        "/v1/engine/team-runs",
        json={
            "manifest": imported.manifest.model_dump(mode="json"),
            "sub_harnesses": {r: dict(s) for r, s in imported.sub_harnesses.items()},
            "gate_decisions": {},
            "graph_id": graph_id,
        },
    )


def _create_graph(c: httpx.Client, name: str) -> str:
    g = c.post("/api/v1/graphs", json={"name": name, "description": "graph-id binding e2e"})
    assert g.status_code == 201, f"create graph failed: {g.status_code} {g.text}"
    return g.json()["id"]


def test_a_run_bound_to_the_callers_own_graph_is_accepted(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """Happy path: a graph the caller's org owns passes the fail-fast check → 202 (worker runs)."""
    user = register(_uniq("owner"))
    c = gateway_client(user["token"])
    graph_id = _create_graph(c, "owned-kb")
    _one_member_team(tmp_path)
    resp = _post_run(c, tmp_path, graph_id)
    assert resp.status_code == 202, f"own graph should be accepted: {resp.status_code} {resp.text}"


def test_a_cross_org_graph_id_is_rejected_fail_fast(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """Org B binds org A's graph_id → the KGS GET (org-scoped by B's identity) 404s → engine 422."""
    owner = register(_uniq("orga"))
    grantee = register(_uniq("orgb"))
    assert owner["org_id"] != grantee["org_id"], "the two users must be in different orgs"

    owner_c = gateway_client(owner["token"])
    a_graph_id = _create_graph(owner_c, "org-a-kb")

    grantee_c = gateway_client(grantee["token"])
    _one_member_team(tmp_path)
    resp = _post_run(grantee_c, tmp_path, a_graph_id)
    assert resp.status_code == 422, (
        f"a cross-org graph_id must be rejected fail-fast: {resp.status_code} {resp.text}"
    )
    assert "graph_id" in resp.text.lower()


def test_an_unknown_graph_id_is_rejected_fail_fast(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    """A graph_id that does not exist anywhere → KGS 404 → engine 422 (never queued)."""
    user = register(_uniq("unk"))
    c = gateway_client(user["token"])
    _one_member_team(tmp_path)
    resp = _post_run(c, tmp_path, str(uuid.uuid4()))
    assert resp.status_code == 422, (
        f"an unknown graph_id must be rejected: {resp.status_code} {resp.text}"
    )
