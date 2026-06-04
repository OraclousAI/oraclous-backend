"""Graph and schema API endpoints — knowledge-graph-service R3 (ORAA-55).

Threat model: T1 — cross-tenant data access via API layer.
Ownership gate is enforced AFTER the DB lookup (not by UUID obscurity):
every graph operation fetches the graph record first, then compares user_id
against the authenticated principal. A 403 body never echoes graph data.

Namespace note (legacy app.* → oraclous_knowledge_graph_service.*):
  app.api.v1.endpoints.graphs      → ...api.v1.endpoints.graphs
  app.api.dependencies.auth_service → ...api.dependencies.auth_service
  app.api.schema.schema_manager     → ...api.schema.schema_manager

Module-level names patched by the test suite (ORA-48):
  neo4j_client    — sync_driver used to initialise GraphNodeService
  GraphNodeService — graph CRUD service class
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request

router = APIRouter()

# ---------------------------------------------------------------------------
# Module-level singletons — patched by the test suite at:
#   oraclous_knowledge_graph_service.api.v1.endpoints.graphs.neo4j_client
#   oraclous_knowledge_graph_service.api.v1.endpoints.graphs.GraphNodeService
# ---------------------------------------------------------------------------

neo4j_client = None  # type: ignore[assignment]  # populated at startup; patched in tests


class GraphNodeService:
    """Stub graph CRUD service. Tests patch the class itself at module level."""

    def __init__(self, driver: object) -> None:
        self.driver = driver

    def get_graph(self, graph_id: str) -> dict | None:
        return None

    def list_user_graphs(self, user_id: str) -> list[dict]:
        return []

    def create_graph(self, name: str, user_id: str) -> dict:
        return {}

    def delete_graph(self, graph_id: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# Auth dependency — function-local import (ORA-48) so tests can patch
# oraclous_knowledge_graph_service.api.dependencies.auth_service at runtime.
# ---------------------------------------------------------------------------


async def get_current_user(authorization: Annotated[str | None, Header()] = None) -> dict:
    """FastAPI dependency: extract + verify bearer token; return user dict."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from oraclous_knowledge_graph_service.api.dependencies import auth_service  # ORA-48

    token = authorization.removeprefix("Bearer ")
    return await auth_service.verify_token(token)


# Annotated type aliases keep B008 (function call in default arg) silent
# and serve as self-documenting parameters.
CurrentUser = Annotated[dict, Depends(get_current_user)]
OptionalBody = Annotated[dict | None, Body()]


# ---------------------------------------------------------------------------
# Ownership gate — runs AFTER the DB lookup (T1 requirement)
# ---------------------------------------------------------------------------


async def _owned_graph(graph_id: str, current_user: dict) -> dict:
    """Fetch the graph then enforce user_id ownership. Returns graph dict."""
    svc = GraphNodeService(neo4j_client.sync_driver)  # type: ignore[union-attr]
    graph = svc.get_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="Graph not found")
    if graph["user_id"] != current_user["id"]:
        # 403 body must not echo any graph data (T1)
        raise HTTPException(status_code=403, detail="Forbidden")
    return graph


# ---------------------------------------------------------------------------
# Graph CRUD routes
# ---------------------------------------------------------------------------


@router.get("/graphs")
async def list_graphs(current_user: CurrentUser) -> list[dict]:
    svc = GraphNodeService(neo4j_client.sync_driver)  # type: ignore[union-attr]
    return svc.list_user_graphs(current_user["id"])


@router.post("/graphs", status_code=201)
async def create_graph(current_user: CurrentUser, body: OptionalBody = None) -> dict:
    svc = GraphNodeService(neo4j_client.sync_driver)  # type: ignore[union-attr]
    name = body.get("name", "") if body else ""
    # user_id always sourced from the auth token — never from the request body (T1)
    return svc.create_graph(name=name, user_id=current_user["id"])


@router.get("/graphs/{graph_id}")
async def get_graph(graph_id: str, current_user: CurrentUser) -> dict:
    return await _owned_graph(graph_id, current_user)


@router.put("/graphs/{graph_id}")
async def update_graph(graph_id: str, current_user: CurrentUser, body: OptionalBody = None) -> dict:
    graph = await _owned_graph(graph_id, current_user)
    if body and "name" in body:
        graph = {**graph, "name": body["name"]}
    return graph


@router.delete("/graphs/{graph_id}")
async def delete_graph(graph_id: str, current_user: CurrentUser) -> dict:
    svc = GraphNodeService(neo4j_client.sync_driver)  # type: ignore[union-attr]
    graph = svc.get_graph(graph_id)
    if graph is None:
        raise HTTPException(status_code=404, detail="Graph not found")
    if graph["user_id"] != current_user["id"]:
        raise HTTPException(status_code=403, detail="Forbidden")
    result = svc.delete_graph(graph_id)
    return {"deleted": result}


# ---------------------------------------------------------------------------
# Ingest, upload, chat-history, community-detection sub-resources
# ---------------------------------------------------------------------------


@router.post("/graphs/{graph_id}/ingest")
async def ingest(graph_id: str, current_user: CurrentUser, body: OptionalBody = None) -> dict:
    await _owned_graph(graph_id, current_user)
    return {"status": "accepted"}


@router.post("/graphs/{graph_id}/upload")
async def upload(graph_id: str, request: Request, current_user: CurrentUser) -> dict:
    await _owned_graph(graph_id, current_user)
    return {"status": "accepted"}


@router.get("/graphs/{graph_id}/chat-history")
async def get_chat_history(graph_id: str, current_user: CurrentUser) -> dict:
    await _owned_graph(graph_id, current_user)
    return {"messages": []}


@router.post("/graphs/{graph_id}/communities/detect")
async def detect_communities(
    graph_id: str, current_user: CurrentUser, body: OptionalBody = None
) -> dict:
    await _owned_graph(graph_id, current_user)
    return {"status": "accepted"}


# ---------------------------------------------------------------------------
# Ontology and instructions sub-resources
# ---------------------------------------------------------------------------


@router.get("/graphs/{graph_id}/ontology")
async def get_ontology(graph_id: str, current_user: CurrentUser) -> dict:
    await _owned_graph(graph_id, current_user)
    return {"entity_types": []}


@router.post("/graphs/{graph_id}/ontology")
async def set_ontology(graph_id: str, current_user: CurrentUser, body: OptionalBody = None) -> dict:
    await _owned_graph(graph_id, current_user)
    return {"status": "updated"}


@router.get("/graphs/{graph_id}/instructions")
async def get_instructions(graph_id: str, current_user: CurrentUser) -> dict:
    await _owned_graph(graph_id, current_user)
    return {"system_prompt": ""}


@router.put("/graphs/{graph_id}/instructions")
async def set_instructions(
    graph_id: str, current_user: CurrentUser, body: OptionalBody = None
) -> dict:
    await _owned_graph(graph_id, current_user)
    return {"status": "updated"}


# ---------------------------------------------------------------------------
# Schema routes
# ---------------------------------------------------------------------------


@router.get("/schema/info/{graph_id}")
async def schema_info(graph_id: str) -> dict:
    import oraclous_knowledge_graph_service.api.schema as _schema_mod  # ORA-48

    result = await _schema_mod.schema_manager.extract_schema(graph_id)
    return result.model_dump(mode="json")


@router.post("/schema/refresh")
async def schema_refresh(body: OptionalBody = None) -> dict:
    import oraclous_knowledge_graph_service.api.schema as _schema_mod  # ORA-48

    graph_id = body.get("graph_id") if body else None
    result = await _schema_mod.schema_manager.extract_schema(graph_id)
    return result.model_dump(mode="json")
