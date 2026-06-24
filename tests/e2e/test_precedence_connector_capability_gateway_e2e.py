"""In-loop precedence ranking via the knowledge-retriever CAPABILITY, e2e via the gateway (#538).

#538 (E6 / ADR-040 — #514's production wire). #536 proved the KRS `/v1/search` precedence ranking;
this proves the RUNTIME honors it: the team binds its Hierarchy of Truth on the knowledge-retriever
INSTANCE config (exactly what the harness `_materialise` writes per member), and a DETERMINISTIC
capability EXECUTION (no agent, no LLM) carries it through the connector → `/v1/search` → #536's
ranking. So a real member's in-loop retrieval is auto-ranked canonical-first — the model never
supplies precedence.

Deterministic + cold-stack-safe: ingest 3 contradicting tiered nodes via #522, then execute the
bound capability and assert the RANK ORDER of the returned hits' `precedence_tier` (bible outranks
drafts; a derived graph node never outranks bible by default; `graph_authoritative` flips it only
when declared). No LLM anywhere. Auto-skips when the gateway is down.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_ORDER = ["rules", "bible", "toc", "drafts"]


def _batch_ingest(c: httpx.Client, graph_id: str, items: list[dict]) -> None:
    resp = c.post(f"/api/v1/graphs/{graph_id}/batch-ingest", json={"items": items})
    assert resp.status_code == 202, f"batch-ingest failed: {resp.status_code} {resp.text}"
    for job in resp.json()["jobs"]:
        for _ in range(60):
            state = str(c.get(f"/api/v1/graphs/{graph_id}/jobs/{job['id']}").json().get("status"))
            if state.upper() in ("SUCCEEDED", "COMPLETED"):
                break
            if state.upper() in ("FAILED", "ERROR"):
                raise AssertionError(f"ingest job {job['id']} failed: {state}")
            time.sleep(2)
        else:
            raise AssertionError(f"ingest job {job['id']} never completed")


def _doc_filenames(c: httpx.Client, graph_id: str) -> set[str]:
    r = c.get(f"/api/v1/graphs/{graph_id}/documents")
    assert r.status_code == 200, r.text
    return {d.get("filename") for d in r.json()}


def _retriever_capability_id(c: httpx.Client) -> str:
    """The seeded knowledge-retriever capability id (core/knowledge-retriever)."""
    r = c.get("/api/v1/capabilities")
    assert r.status_code == 200, r.text
    caps = r.json()["capabilities"]
    for cap in caps:
        haystack = (
            " ".join(str(cap.get(k) or "") for k in ("name", "ref", "identifier", "category"))
            .lower()
            .replace(" ", "-")
            .replace("_", "-")
        )
        if "knowledge-retriever" in haystack:  # matches the "Knowledge Retriever" name + ref slug
            return str(cap["id"])
    names = [cap.get("name") for cap in caps]
    raise AssertionError(f"knowledge-retriever capability not found among {names}")


def _bound_instance(
    c: httpx.Client, capability_id: str, graph_id: str, *, authoritative: bool
) -> str:
    """Create a retriever instance with the team's precedence bound on its config — exactly what the
    harness ``_materialise`` writes per member. Instances are config-immutable; the authoritative
    flip uses a second instance."""
    r = c.post(
        "/api/v1/instances",
        json={
            "capability_id": capability_id,
            "name": f"kr-prec-{uuid.uuid4().hex[:8]}",
            "configuration": {
                "graph_id": graph_id,
                "precedence": {"order": _ORDER, "graph_authoritative": authoritative},
            },
        },
    )
    assert r.status_code in (200, 201), f"instance create failed: {r.status_code} {r.text}"
    return str(r.json()["id"])


def _execute_hits(c: httpx.Client, instance_id: str, query: str) -> list[dict]:
    """Execute the bound capability DETERMINISTICALLY (no LLM) → the ranked hit list."""
    r = c.post(
        f"/api/v1/instances/{instance_id}/execute",
        json={"input_data": {"operation": "search", "query": query, "mode": "hybrid", "top_k": 20}},
    )
    assert r.status_code in (200, 201), f"execute failed: {r.status_code} {r.text}"
    payload = r.json()
    assert payload.get("status") == "SUCCESS", f"capability execution did not succeed: {payload}"
    out = payload.get("output_data") or {}
    return out.get("hits") or []


def _tier_index(hits: list[dict], tier: str) -> int | None:
    for i, h in enumerate(hits):
        if h.get("properties", {}).get("precedence_tier") == tier:
            return i
    return None


def _await_tiers(c: httpx.Client, instance_id: str, query: str, tiers: set[str]) -> list[dict]:
    """Retry the capability execution until every wanted tier is present (cold-stack :Chunk lag),
    then return the ranked hits — the ORDER is deterministic once the hits land."""
    hits: list[dict] = []
    for _ in range(40):
        hits = _execute_hits(c, instance_id, query)
        if all(_tier_index(hits, t) is not None for t in tiers):
            return hits
        time.sleep(2)
    present = {h.get("properties", {}).get("precedence_tier") for h in hits}
    raise AssertionError(f"tiers {tiers} not all retrievable (cold-stack lag); present={present}")


def test_a_bound_member_retrieval_is_auto_ranked_canonical_first(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"prconn{uuid.uuid4().hex[:10]} owner")  # unique slug → fresh personal org
    c = gateway_client(user["token"])
    graph_id = c.post("/api/v1/graphs", json={"name": "prec-conn-kb"}).json()["id"]

    nonce = uuid.uuid4().hex[:8]
    _batch_ingest(
        c,
        graph_id,
        [
            {"path": "bible/canon.md", "content": f"Canon {nonce}: the hero SURVIVES the war."},
            {"path": "drafts/ch1.md", "content": f"Draft {nonce}: the hero DIES in the war."},
            {"path": "scratch/auto.md", "content": f"Note {nonce}: the hero VANISHES in the war."},
        ],
    )
    landed = _doc_filenames(c, graph_id)
    assert {"bible/canon.md", "drafts/ch1.md", "scratch/auto.md"} <= landed, f"missing: {landed}"

    cap_id = _retriever_capability_id(c)

    # default (graph: derived) — a member's bound retrieval ranks canonical-first
    inst = _bound_instance(c, cap_id, graph_id, authoritative=False)
    hits = _await_tiers(c, inst, nonce, {"bible", "drafts", "graph"})
    bible_i, drafts_i, graph_i = (_tier_index(hits, t) for t in ("bible", "drafts", "graph"))
    assert bible_i < drafts_i, f"bible must outrank drafts via the bound capability: {hits}"
    assert bible_i < graph_i, "a derived graph node must NEVER outrank bible by default"

    # graph_authoritative — a second bound instance flips the derived graph above the file tier
    inst_auth = _bound_instance(c, cap_id, graph_id, authoritative=True)
    auth = _await_tiers(c, inst_auth, nonce, {"bible", "graph"})
    assert _tier_index(auth, "graph") < _tier_index(auth, "bible"), (
        "graph outranks bible ONLY when the team declares graph_authoritative"
    )
