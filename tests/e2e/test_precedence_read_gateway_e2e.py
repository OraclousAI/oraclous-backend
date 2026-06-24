"""Read-side Hierarchy-of-Truth precedence ranking END-TO-END through the GATEWAY (#514, E6).

Deterministic, NO LLM: ingest CONTRADICTING tiered nodes (``bible/canon.md`` vs ``drafts/ch1.md`` vs
a derived ``scratch/auto.md``) via the #522 batch-ingest, then read them back through the gateway's
knowledge-retriever ``/v1/search`` with the team's declared Hierarchy of Truth — and assert the
canonical (``bible``) hit OUTRANKS the lower-tier (``drafts``) hit, a derived (``graph``) node never
outranks ``bible`` by default, and ``graph_authoritative`` flips that only when declared. Tier is
PATH-derived (from each node's ``ingestion_source``); the ranking is deterministic; the assertion is
the RANK ORDER (not a fuzzy search match), so it is robust on a cold stack (retry until the hits
land, then their relative order is deterministic). The #514 read acceptance, no LLM in the path.

No fakes: real registration → real gateway → real KGS ingest worker → real Neo4j → real
knowledge-retriever search, all through the gateway. Key-free (text lands as Document/Chunk;
HashingEmbedder); CI-runnable on the cold stack. Auto-skips when the gateway is down.
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
    """Batch-ingest the tiered files through the gateway; wait each job to terminal (cold-start)."""
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


def _ranked_search(
    c: httpx.Client, graph_id: str, query: str, *, graph_authoritative: bool = False
) -> list[dict]:
    """One precedence-aware hybrid read through the gateway → the ranked hit list."""
    r = c.post(
        "/v1/search/hybrid",
        json={
            "query": query,
            "graph_id": graph_id,
            "top_k": 20,
            "precedence": {"order": _ORDER, "graph_authoritative": graph_authoritative},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _tier_index(hits: list[dict], tier: str) -> int | None:
    """The position of the first hit stamped with ``precedence_tier == tier`` (None if absent)."""
    for i, h in enumerate(hits):
        if h.get("properties", {}).get("precedence_tier") == tier:
            return i
    return None


def _await_tiers(
    c: httpx.Client,
    graph_id: str,
    query: str,
    tiers: set[str],
    *,
    graph_authoritative: bool = False,
) -> list[dict]:
    """Retry the precedence read until every wanted tier is present (cold-stack :Chunk-scan lag),
    then return the ranked hits — the ORDER is deterministic once the hits land."""
    hits: list[dict] = []
    for _ in range(40):
        hits = _ranked_search(c, graph_id, query, graph_authoritative=graph_authoritative)
        if all(_tier_index(hits, t) is not None for t in tiers):
            return hits
        time.sleep(2)
    present = {h.get("properties", {}).get("precedence_tier") for h in hits}
    raise AssertionError(f"tiers {tiers} not all retrievable (cold-stack lag); present={present}")


def test_canonical_tier_outranks_lower_and_derived_tiers_on_the_read(
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"prec{uuid.uuid4().hex[:10]} owner")  # unique slug → fresh personal-org
    c = gateway_client(user["token"])
    graph_id = c.post("/api/v1/graphs", json={"name": "precedence-kb"}).json()["id"]

    nonce = uuid.uuid4().hex[:8]
    # three CONTRADICTING facts about the same subject, at three tiers (path-derived):
    #   bible/ (canonical) vs drafts/ (lower file tier) vs scratch/ (no declared layer → graph)
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

    # default (graph: derived) — canonical outranks every lower/derived tier
    hits = _await_tiers(c, graph_id, nonce, {"bible", "drafts", "graph"})
    bible_i, drafts_i, graph_i = (_tier_index(hits, t) for t in ("bible", "drafts", "graph"))
    assert bible_i < drafts_i, (
        f"bible must outrank drafts: {[h['properties'].get('precedence_tier') for h in hits]}"
    )
    assert bible_i < graph_i, (
        "a derived graph node must NEVER outrank bible (derived-not-canonical)"
    )

    # graph_authoritative — declared, so the derived graph node may now outrank a file tier
    auth = _await_tiers(c, graph_id, nonce, {"bible", "graph"}, graph_authoritative=True)
    assert _tier_index(auth, "graph") < _tier_index(auth, "bible"), (
        "graph must outrank bible ONLY when graph_authoritative is declared"
    )
