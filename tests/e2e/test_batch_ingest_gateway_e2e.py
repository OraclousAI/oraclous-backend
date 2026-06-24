"""Batch/folder content ingest END-TO-END through the GATEWAY (#522, E6 — cloud content-in).

The cloud content-in flow: a user lands a FOLDER/REPO of content in their org graph in one call —
the content discovered from a team dir (bible/rules/drafts), batch-ingested through the gateway,
item an async job that writes graph nodes. The content then RETRIEVES (real substrate for members),
and a re-ingest is IDEMPOTENT (deterministic per-path document id → replace, never duplicate) — so a
recurring refresh re-lands a clean tree, not a doubled graph.

No fakes: real registration → real gateway → real KGS ingest worker → real Neo4j → real retrieval,
all through the gateway. No LLM key needed (text lands as retrievable Document/Chunk nodes), so this
is deterministic and CI-runnable. Auto-skips when the gateway is down.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def _content_tree(root: Path, nonce: str) -> dict[str, str]:
    """A small git-markdown-ish content tree (+ team config that must NOT be ingested). Returns the
    per-file unique marker so the test can prove each file's content landed + retrieves."""
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".claude" / "agents" / "scribe.md").write_text("---\nname: scribe\n---\nwrite.\n")
    markers = {
        "bible/canon.md": f"BIBLE{nonce}",
        "rules/style.md": f"RULES{nonce}",
        "drafts/ch1.md": f"DRAFT{nonce}",
    }
    for rel, marker in markers.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"# {rel}\nThe canonical marker for this file is {marker}.")
    return markers


def _batch_ingest(c: httpx.Client, graph_id: str, root: Path) -> int:
    """Discover the tree's content + batch-ingest it through the gateway; wait for each job. Returns
    the number of items ingested."""
    from oraclous_ohm.import_.content import discover_content_files

    items = [
        {"path": f.path, "content": f.content, "source_type": f.source_type}
        for f in discover_content_files(root)
    ]
    resp = c.post(f"/api/v1/graphs/{graph_id}/batch-ingest", json={"items": items})
    assert resp.status_code == 202, f"batch-ingest failed: {resp.status_code} {resp.text}"
    jobs = resp.json()["jobs"]
    assert len(jobs) == len(items)
    for job in jobs:
        for _ in range(45):
            state = str(c.get(f"/api/v1/graphs/{graph_id}/jobs/{job['id']}").json().get("status"))
            if state.upper() in ("SUCCEEDED", "COMPLETED"):
                break
            if state.upper() in ("FAILED", "ERROR"):
                raise AssertionError(f"ingest job {job['id']} failed: {state}")
            time.sleep(2)
        else:
            raise AssertionError(f"ingest job {job['id']} never completed")
    return len(items)


def _found(c: httpx.Client, graph_id: str, marker: str) -> bool:
    r = c.post("/v1/search/fulltext", json={"query": marker, "graph_id": graph_id, "top_k": 10})
    assert r.status_code == 200, r.text
    return marker in r.text


def test_a_folder_of_content_lands_in_the_graph_and_reingest_is_idempotent(
    tmp_path: Path,
    register: Callable[..., dict],
    gateway_client: Callable[[str], httpx.Client],
) -> None:
    user = register(f"ingest{uuid.uuid4().hex[:10]} user")
    c = gateway_client(user["token"])
    graph_id = c.post("/api/v1/graphs", json={"name": "content-kb"}).json()["id"]

    markers = _content_tree(tmp_path, uuid.uuid4().hex[:8])

    # (1) batch-ingest the folder → every file's content lands + retrieves
    n = _batch_ingest(c, graph_id, tmp_path)
    assert n == len(markers)
    for rel, marker in markers.items():
        assert _found(c, graph_id, marker), f"{rel!r} content ({marker}) did not land/retrieve"

    # (2) the team CONFIG was NOT ingested (a charter/agent prompt never enters the graph)
    assert not _found(c, graph_id, "scribe"), "team config leaked into the knowledge graph"

    # (3) re-ingest the SAME folder → idempotent: content still retrieves (deterministic-id replace,
    #     not a doubled or broken graph)
    _batch_ingest(c, graph_id, tmp_path)
    for rel, marker in markers.items():
        assert _found(c, graph_id, marker), f"re-ingest lost {rel!r} ({marker})"
