"""The citation data path END-TO-END through the GATEWAY (#742, §CITE rev3) — NO fakes.

A real user, through the application-gateway on ``:8006`` only, brings their own GitHub token and
their own repository: store the PAT, create a GitHub Reader instance, execute ``read_file`` against
a real file, pass the returned ``source`` **unmodified** into ingest, and read the record back with
a typed, resolvable ``citation`` on it. No service port, no ``/internal``, no DB-direct assertion,
nothing mocked or monkeypatched. No LLM is involved on this path.

Issue #742 acceptance criteria 15-20, one test each:

* 15 — the positive path: connector source → ingest → a typed citation on the retrieval hit.
* 16 — the url OPENS the file: an unauthenticated ``HEAD`` of the returned url returns 200. That is
  what turns "resolvable" from a claim into a proof.
* 17 — same document, one id: every chunk of one document carries the same ``citation_id``.
* 18 — forgery (threat T1): a caller-supplied citation never survives, on either boundary.
* 19 — determinism and supersession: same revision → same id; changed content with no inbound
  revision → a different id, stamped ``content_hash``.
* 20 — the null case: a plain ingest with no source is stamped an ``upload`` citation.

Criteria 15-19 need a REAL GitHub PAT with Contents read, so they carry the ``github`` marker and
skip without one; criterion 20 is key-free. The whole package auto-skips when the gateway is down.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]

_PAT = os.environ.get("GITHUB_DELIVER_PAT")
_REPO = os.environ.get("GITHUB_DELIVER_REPO")

#: A real file in the caller's own repository. Long enough to chunk, so criterion 17 has several
#: hits to compare rather than one.
_FILE_PATH = "CLAUDE.md"

requires_github = pytest.mark.skipif(
    not (_PAT and _REPO),
    reason="GITHUB_DELIVER_PAT/GITHUB_DELIVER_REPO unset — criteria 15-19 need a real PAT + repo",
)


# --------------------------------------------------------------------------------------------
# Gateway helpers — every one of them a public API call the user themselves could make
# --------------------------------------------------------------------------------------------


def _store_pat(c: httpx.Client, user_id: str) -> str:
    """The user brings their own token, through the public credentials API."""
    r = c.post(
        "/credentials/",
        json={
            "tool_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": f"gh-{uuid.uuid4().hex[:6]}",
            "provider": "github",
            "cred_type": "api_key",
            "credential": {"api_key": _PAT},
        },
    )
    assert r.status_code == 201, r.text
    assert _PAT not in r.text, "the PAT was echoed back — it must be KMS-sealed"
    return str(r.json()["id"])


def _github_instance(c: httpx.Client, credential_id: str) -> str:
    caps = {x["name"]: x for x in c.get("/api/v1/capabilities").json()["capabilities"]}
    assert "GitHub Reader" in caps, sorted(caps)
    inst = c.post(
        "/api/v1/instances",
        json={
            "capability_id": caps["GitHub Reader"]["id"],
            "name": f"gh-reader-{uuid.uuid4().hex[:6]}",
            "configuration": {},
        },
    )
    assert inst.status_code in (200, 201), inst.text
    instance_id = str(inst.json()["id"])
    conf = c.post(
        f"/api/v1/instances/{instance_id}/configure-credentials",
        json={"credential_mappings": {"api_key": credential_id}},
    )
    assert conf.status_code in (200, 201), conf.text
    return instance_id


def _read_file(c: httpx.Client, instance_id: str, path: str = _FILE_PATH) -> dict:
    """Execute the connector's ``read_file`` and return its ``{path, content, source}`` payload."""
    r = c.post(
        f"/api/v1/instances/{instance_id}/execute",
        json={"input_data": {"operation": "read_file", "repo": _REPO, "path": path}},
    )
    assert r.status_code in (200, 201, 202), r.text
    body = r.json() or {}
    assert str(body.get("status")).upper() == "SUCCESS", r.text[:400]
    data = body.get("output_data") or {}
    assert data.get("content"), f"the connector returned no content: {r.text[:400]}"
    assert data.get("source"), f"the connector emitted no source: {r.text[:400]}"
    return dict(data)


def _new_graph(c: httpx.Client) -> str:
    g = c.post("/api/v1/graphs", json={"name": f"cite-kb-{uuid.uuid4().hex[:6]}"})
    assert g.status_code == 201, g.text
    return str(g.json()["id"])


def _ingest(c: httpx.Client, graph_id: str, body: dict) -> str:
    """POST /ingest, wait the async job to terminal, return the job id."""
    r = c.post(f"/api/v1/graphs/{graph_id}/ingest", json=body)
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


def _search(c: httpx.Client, graph_id: str, query: str) -> list[dict]:
    r = c.post("/v1/search/hybrid", json={"query": query, "graph_id": graph_id, "top_k": 25})
    assert r.status_code == 200, r.text
    return list(r.json())


def _await_cited_hits(c: httpx.Client, graph_id: str, query: str) -> list[dict]:
    """Retry the read until the freshly written chunks are visible, then return the CITED hits."""
    hits: list[dict] = []
    for _ in range(40):
        hits = _search(c, graph_id, query)
        cited = [h for h in hits if h.get("citation")]
        if cited:
            return cited
        time.sleep(2)
    raise AssertionError(f"no hit carried a citation after ingest; last hits={hits[:2]}")


# --------------------------------------------------------------------------------------------
# 15 — the positive path
# --------------------------------------------------------------------------------------------


@requires_github
@pytest.mark.github
def test_15_a_github_read_lands_a_typed_citation_on_the_retrieval_hit(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"cite{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    read = _read_file(c, _github_instance(c, _store_pat(c, user["user_id"])))
    source = read["source"]

    graph_id = _new_graph(c)
    nonce = uuid.uuid4().hex[:8]
    # The source is passed through UNMODIFIED — the connector fills it, and only the connector.
    _ingest(
        c,
        graph_id,
        {
            "content": f"{read['content']}\n\nCitation probe {nonce}.\n",
            "filename": _FILE_PATH,
            "source": source,
        },
    )

    citation = _await_cited_hits(c, graph_id, nonce)[0]["citation"]
    assert citation["source_system"] == "github"
    assert citation["source_id"] == f"{_REPO}:{_FILE_PATH}"
    assert citation["revision"] == source["revision"], "the blob sha must survive to the record"
    assert citation["revision_kind"] == "blob_sha"
    assert citation["url"], "a github citation must be openable"
    assert citation["citation_id"].startswith("cit_")
    assert citation["retrieved_at"], "retrieved_at is server-computed and always present"


# --------------------------------------------------------------------------------------------
# 16 — the url opens the file
# --------------------------------------------------------------------------------------------


@requires_github
@pytest.mark.github
def test_16_the_returned_citation_url_opens_the_file(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"citeurl{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    read = _read_file(c, _github_instance(c, _store_pat(c, user["user_id"])))

    graph_id = _new_graph(c)
    nonce = uuid.uuid4().hex[:8]
    _ingest(
        c,
        graph_id,
        {
            "content": f"{read['content']}\n\nCitation probe {nonce}.\n",
            "filename": _FILE_PATH,
            "source": read["source"],
        },
    )
    url = _await_cited_hits(c, graph_id, nonce)[0]["citation"]["url"]

    # Fetched WITHOUT our token: "resolvable" means a person can open it, not that we can.
    opened = httpx.head(url, follow_redirects=True, timeout=30.0)
    assert opened.status_code == 200, f"citation.url did not open: {url} -> {opened.status_code}"


# --------------------------------------------------------------------------------------------
# 17 — same document, one id
# --------------------------------------------------------------------------------------------


@requires_github
@pytest.mark.github
def test_17_every_chunk_of_one_document_carries_the_same_citation_id(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"citeone{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    read = _read_file(c, _github_instance(c, _store_pat(c, user["user_id"])))

    graph_id = _new_graph(c)
    nonce = uuid.uuid4().hex[:8]
    # One nonce per paragraph, so a multi-chunk document really returns several hits.
    probe = "\n\n".join(f"Citation probe {nonce} paragraph {i}." for i in range(12))
    _ingest(
        c,
        graph_id,
        {
            "content": f"{read['content']}\n\n{probe}\n",
            "filename": _FILE_PATH,
            "source": read["source"],
        },
    )

    cited = _await_cited_hits(c, graph_id, nonce)
    assert len(cited) > 1, f"expected a multi-chunk document, got {len(cited)} cited hit(s)"
    assert len({h["citation"]["citation_id"] for h in cited}) == 1, (
        "one document at one version is ONE citation — every chunk must share the id"
    )


# --------------------------------------------------------------------------------------------
# 18 — forgery (threat T1)
# --------------------------------------------------------------------------------------------


@requires_github
@pytest.mark.github
@pytest.mark.security
def test_18_a_caller_supplied_citation_never_survives_the_write(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """The property the whole Contract exists for, proven through the real path.

    Two boundaries, both asserted: a forged citation smuggled alongside a legitimate body is
    replaced by the server-stamped value, and platform-computed provenance offered on the inbound
    ``source`` is refused outright rather than silently dropped.
    """
    user = register(f"citeforge{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    read = _read_file(c, _github_instance(c, _store_pat(c, user["user_id"])))

    graph_id = _new_graph(c)
    nonce = uuid.uuid4().hex[:8]
    forged_id = "cit_" + "f" * 32
    _ingest(
        c,
        graph_id,
        {
            "content": f"{read['content']}\n\nCitation probe {nonce}.\n",
            "filename": _FILE_PATH,
            "source": read["source"],
            # The forgery: a caller claiming a different source, alongside a legitimate one.
            "citation": {"citation_id": forged_id, "source_system": "internal-legal"},
            "citation_id": forged_id,
            "source_system": "internal-legal",
            "source_id": "legal/partner-agreement.md",
            "source_url": "https://evil.example.com/partner-agreement",
        },
    )

    hit = _await_cited_hits(c, graph_id, nonce)[0]
    citation = hit["citation"]
    assert citation["citation_id"] != forged_id, "the forged id survived the write"
    assert citation["source_system"] == "github"
    assert citation["source_id"] == f"{_REPO}:{_FILE_PATH}"
    blob = repr(hit)
    for forged in (forged_id, "internal-legal", "legal/partner-agreement.md", "evil.example.com"):
        assert forged not in blob, f"forged value {forged!r} reached the reader: {blob[:400]}"

    # The other boundary: the platform mints citation_id and retrieved_at, so a source offering
    # either is refused rather than silently ignored.
    for planted in ("citation_id", "retrieved_at"):
        refused = c.post(
            f"/api/v1/graphs/{graph_id}/ingest",
            json={
                "content": "forged provenance",
                "filename": "forged.md",
                "source": {**read["source"], planted: forged_id},
            },
        )
        assert refused.status_code == 422, (
            f"a source carrying {planted} was accepted: {refused.status_code} {refused.text[:300]}"
        )


# --------------------------------------------------------------------------------------------
# 19 — determinism and supersession
# --------------------------------------------------------------------------------------------


@requires_github
@pytest.mark.github
def test_19_the_same_revision_repeats_its_id_and_changed_content_supersedes(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    user = register(f"citedet{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    read = _read_file(c, _github_instance(c, _store_pat(c, user["user_id"])))
    source = read["source"]

    # (a) the identical file at the identical revision, ingested twice, into two graphs
    ids = []
    for _ in range(2):
        graph_id = _new_graph(c)
        nonce = uuid.uuid4().hex[:8]
        _ingest(
            c,
            graph_id,
            {
                "content": f"{read['content']}\n\nCitation probe {nonce}.\n",
                "filename": _FILE_PATH,
                "source": source,
            },
        )
        ids.append(_await_cited_hits(c, graph_id, nonce)[0]["citation"]["citation_id"])
    assert ids[0] == ids[1], "re-reading an unchanged revision must not mint a new record"

    # (b) changed content with NO inbound revision — the content-hash fallback supersedes
    bare = {k: v for k, v in source.items() if k not in ("revision", "revision_kind")}
    hashed = []
    for body in ("Revenue target is 4M.", "Revenue target is 5M."):
        graph_id = _new_graph(c)
        nonce = uuid.uuid4().hex[:8]
        _ingest(
            c,
            graph_id,
            {
                "content": f"{body}\n\nCitation probe {nonce}.\n",
                "filename": "plan.md",
                "source": bare,
            },
        )
        citation = _await_cited_hits(c, graph_id, nonce)[0]["citation"]
        assert citation["revision_kind"] == "content_hash", citation
        hashed.append(citation["citation_id"])
    assert hashed[0] != hashed[1], "changed content must supersede — a new revision is a new id"


# --------------------------------------------------------------------------------------------
# 20 — the null case (key-free)
# --------------------------------------------------------------------------------------------


def test_20_an_ingest_with_no_source_is_stamped_an_upload_citation(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    """No connector, no source — the platform still knows exactly what it received.

    ``url`` is null because no route serves an uploaded document back yet. Claiming a URL that does
    not resolve would be the same fiction the Contract exists to reject.
    """
    user = register(f"citeup{uuid.uuid4().hex[:10]} owner")
    c = gateway_client(user["token"])
    graph_id = _new_graph(c)
    nonce = uuid.uuid4().hex[:8]

    job_id = _ingest(
        c,
        graph_id,
        {
            "content": f"An uploaded plan. Citation probe {nonce}.\n",
            "filename": "quarterly-plan.md",
        },
    )

    citation = _await_cited_hits(c, graph_id, nonce)[0]["citation"]
    assert citation["source_system"] == "upload"
    assert citation["source_id"] == job_id, "an upload citation resolves to its ingest job"
    assert citation["url"] is None
    assert citation["revision_kind"] == "content_hash"
