"""ArtifactsClient (ADR-043 #552) — the engine's org-scoped read of a graph's LANDED artifacts,
used by the coded loop done-check to confirm a loop's work actually persisted. Mirrors GraphClient:
GET /v1/artifacts?graph_id=<id> with the downstream headers (KGS scopes by org); a 404 is an empty
list (nothing landed for this caller); any other non-2xx is inconclusive → a fail-closed raise.

#1137 extends the same client for the platform's own settle-time save: ``list_artifacts`` grows
``team_run_id``/``member_role`` filters (the duplicate guard's one read, scoped to the run+role
that is about to be written), and a new ``ingest()`` sends the platform-built document to the KGS's
``/internal/v1/ingest`` (the same route ``core/graph-ingest`` already POSTs for the tool path,
Layer-3-to-Layer-1, ADR-018). ``ingest()`` forwards only the six wire fields the internal route and
``GraphIngestConnector._producer_config`` already agree on (``producer_kind``, ``team_run_id``,
``member_role``, ``execution_id``, ``team_id``, ``ordinal``) — never a caller-supplied extra key —
and, like ``list_artifacts``, never leaks an upstream error body into its own exception (no-leak).
A non-2xx here is a genuine write failure (unlike ``list_artifacts``' 404-is-empty reading), so it
always raises ``ArtifactsClientError`` rather than swallowing it.

RED until ``artifacts_client`` lands — imported function-locally so the module still collects.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import httpx
import pytest

pytestmark = pytest.mark.unit


def _client(
    handler: Callable[[httpx.Request], httpx.Response], headers: dict[str, str] | None = None
):
    from oraclous_execution_engine_service.services.artifacts_client import ArtifactsClient

    return ArtifactsClient(
        "http://kgs",
        headers=headers or {"X-Internal-Key": "k"},
        transport=httpx.MockTransport(handler),
    )


async def test_lists_artifacts_org_scoped_by_graph_id_query() -> None:
    gid = uuid.uuid4()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["graph_id"] = request.url.params.get("graph_id")
        seen["key"] = request.headers.get("X-Internal-Key")
        return httpx.Response(200, json=[{"id": str(uuid.uuid4()), "filename": "draft.md"}])

    arts = await _client(handler).list_artifacts(gid)
    assert len(arts) == 1
    assert seen["path"] == "/v1/artifacts"
    assert seen["graph_id"] == str(gid)  # org-scoped read keyed on the bound graph
    assert seen["key"] == "k"  # downstream identity headers passed through


async def test_404_is_an_empty_list_not_an_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    # a graph that does not exist OR belongs to another org → nothing landed for this caller
    assert await _client(handler).list_artifacts(uuid.uuid4()) == []


async def test_non_2xx_raises_fail_closed() -> None:
    from oraclous_execution_engine_service.services.artifacts_client import ArtifactsClientError

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="kaboom")

    with pytest.raises(ArtifactsClientError):
        await _client(handler).list_artifacts(uuid.uuid4())


async def test_transport_error_raises_fail_closed() -> None:
    from oraclous_execution_engine_service.services.artifacts_client import ArtifactsClientError

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kgs unreachable")

    with pytest.raises(ArtifactsClientError):
        await _client(handler).list_artifacts(uuid.uuid4())


# ── #1137: list_artifacts grows the duplicate guard's run+role filter ───────────────────────────


async def test_lists_artifacts_filtered_by_team_run_id_and_member_role() -> None:
    gid = uuid.uuid4()
    run_id = uuid.uuid4()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    arts = await _client(handler).list_artifacts(gid, team_run_id=run_id, member_role="analyst")

    assert arts == []
    assert seen["params"]["graph_id"] == str(gid)
    assert seen["params"]["team_run_id"] == str(run_id)
    assert seen["params"]["member_role"] == "analyst"


async def test_list_artifacts_omits_the_new_filters_when_not_given() -> None:
    """An unrelated caller (the existing #552 done-check) must see byte-for-byte the same request
    it always has — no ``team_run_id=None``/``member_role=None`` literal added to the query."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[])

    await _client(handler).list_artifacts(uuid.uuid4())

    assert "team_run_id" not in seen["params"]
    assert "member_role" not in seen["params"]


# ── #1137: ingest() — the platform's own send to the KGS internal ingest route ──────────────────

_PRODUCER = {
    "producer_kind": "team-member",
    "member_role": "analyst",
    "team_run_id": "6332197e-0b1b-4845-bd9e-230a27bbaf38",
    "team_id": "22222222-2222-2222-2222-222222222222",
    "execution_id": "33333333-3333-3333-3333-333333333333",
    "ordinal": None,  # a plain single dispatch — never sent as a literal null
}


async def test_ingest_posts_the_document_to_the_internal_ingest_route() -> None:
    gid = uuid.uuid4()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers.get("X-Internal-Key")
        return httpx.Response(202, json={"id": str(uuid.uuid4()), "status": "pending"})

    job = await _client(handler).ingest(
        gid, content='{"posture": "ready"}', source_type="text", producer=_PRODUCER
    )

    assert seen["path"] == "/internal/v1/ingest"
    body = seen["body"]
    assert body["graph_id"] == str(gid)
    assert body["content"] == '{"posture": "ready"}'
    assert body["source_type"] == "text"
    assert "title" not in body  # #1137: the platform's own save never sets a title
    # exactly the six wire fields the internal route + the tool-path connector already agree on
    assert body["producer_kind"] == "team-member"
    assert body["member_role"] == "analyst"
    assert body["team_run_id"] == _PRODUCER["team_run_id"]
    assert body["team_id"] == _PRODUCER["team_id"]
    assert body["execution_id"] == _PRODUCER["execution_id"]
    assert "ordinal" not in body  # a None value is dropped, never sent as a literal null
    assert seen["key"] == "k"  # downstream identity headers passed through
    assert job["id"]


async def test_ingest_drops_an_unrecognised_producer_key() -> None:
    """The producer dict a caller hands in is never forwarded verbatim — only the six wire fields
    the internal route actually reads are sent, so a stray key never rides along unnoticed."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"id": str(uuid.uuid4()), "status": "pending"})

    producer = {**_PRODUCER, "attempt_id": "not-a-wire-field"}
    await _client(handler).ingest(uuid.uuid4(), content="hi", source_type="text", producer=producer)

    assert "attempt_id" not in seen["body"]


async def test_ingest_forwards_the_callers_verified_organisation_headers() -> None:
    """The same downstream-header mechanism ``list_artifacts`` already uses (built by the caller
    from ``build_downstream_headers``) carries the org/principal identity onto the ingest POST
    too — the client itself never re-derives or re-checks identity."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["org"] = request.headers.get("X-Organisation-Id")
        seen["principal"] = request.headers.get("X-Principal-Id")
        return httpx.Response(202, json={"id": str(uuid.uuid4()), "status": "pending"})

    client = _client(
        handler,
        headers={
            "X-Internal-Key": "k",
            "X-Organisation-Id": "22222222-2222-2222-2222-222222222222",
            "X-Principal-Id": "11111111-1111-1111-1111-111111111111",
        },
    )
    await client.ingest(uuid.uuid4(), content="hi", source_type="text")

    assert seen["org"] == "22222222-2222-2222-2222-222222222222"
    assert seen["principal"] == "11111111-1111-1111-1111-111111111111"


async def test_ingest_non_2xx_raises_fail_closed_and_does_not_leak_the_body() -> None:
    """Unlike ``list_artifacts``' 404-is-empty reading, a non-2xx on a WRITE is a genuine failure
    (e.g. the graph vanished between the settle decision and the send) — it always raises, and the
    upstream body (which can carry another org's identifiers) is never echoed into the message."""
    from oraclous_execution_engine_service.services.artifacts_client import ArtifactsClientError

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "graph 11111111 not found for org SECRET"})

    with pytest.raises(ArtifactsClientError) as exc_info:
        await _client(handler).ingest(uuid.uuid4(), content="hi", source_type="text")

    assert "SECRET" not in str(exc_info.value)


async def test_ingest_transport_error_raises_fail_closed() -> None:
    from oraclous_execution_engine_service.services.artifacts_client import ArtifactsClientError

    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kgs unreachable")

    with pytest.raises(ArtifactsClientError):
        await _client(handler).ingest(uuid.uuid4(), content="hi", source_type="text")
