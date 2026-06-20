"""Knowledge journey END-TO-END through the API GATEWAY — NO fakes.

A real user, through the gateway, builds a knowledge graph and retrieves from it: create a graph,
add a memory (a fact), then search it back — exercising the real knowledge-graph service, real
Neo4j, and the real embedder. And a graph is org-isolated: another user cannot see it. Nothing
mocked, nothing DB-direct; assertions are on what the user observes through the API.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


def test_a_user_builds_a_knowledge_graph_and_retrieves_a_memory(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    c = gateway_client(register("KB Owner")["token"])

    # create a graph
    g = c.post("/api/v1/graphs", json={"name": "my-kb", "description": "facts"})
    assert g.status_code == 201, g.text
    gid = g.json()["id"]

    # add a memory (a fact)
    fact = "The Eiffel Tower is in Paris."
    m = c.post(
        f"/api/v1/graphs/{gid}/memories",
        json={
            "type": "semantic",
            "content": fact,
            "subject": "Eiffel Tower",
            "predicate": "located_in",
            "object": "Paris",
        },
    )
    assert m.status_code == 201, m.text

    # retrieve it through the real search path (KGS + Neo4j + embedder)
    s = c.get(f"/api/v1/graphs/{gid}/memories/search", params={"query": "Eiffel Tower"})
    assert s.status_code == 200, s.text
    contents = [mem["content"] for mem in s.json()["memories"]]
    assert fact in contents, contents


def test_a_knowledge_graph_is_org_isolated_through_the_gateway(
    register: Callable[..., dict], gateway_client: Callable[[str], httpx.Client]
) -> None:
    gid = (
        gateway_client(register("KB A")["token"])
        .post("/api/v1/graphs", json={"name": "a", "description": "x"})
        .json()["id"]
    )
    other = gateway_client(register("KB B")["token"])
    assert other.get(f"/api/v1/graphs/{gid}").status_code == 404  # B cannot see A's graph
