"""Neo4j connection (core layer — connection setup).

A single sync `neo4j.Driver` opened as the read role from `KRS_NEO4J_*`. KRS is strictly
READ-ONLY (T6): it never issues write Cypher and never creates indexes — schema (any
fulltext index included) is owned by the write side (knowledge-graph-service). The fulltext modality
therefore uses an index-free `CONTAINS` scan, org-scoped in-query (Community lacks RLS).
"""

from __future__ import annotations

from neo4j import AsyncDriver, AsyncGraphDatabase, Driver, GraphDatabase

from oraclous_knowledge_retriever_service.core.config import Settings


class Neo4jUnconfiguredError(RuntimeError):
    """KRS_NEO4J_URI is not set — retrieval is unavailable."""


def make_neo4j_driver(settings: Settings) -> Driver:
    if not settings.neo4j_uri:
        raise Neo4jUnconfiguredError("KRS_NEO4J_URI is not set")
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    driver.verify_connectivity()
    return driver


async def make_neo4j_async_driver(settings: Settings) -> AsyncDriver:
    """An async Neo4j driver for the ReBAC engine (it requires ``neo4j.AsyncDriver``); the read path
    stays on the sync driver. Used ONLY to resolve the cross-org access decision (a HAS_ROLE lookup)
    — KRS issues no write Cypher (T6). Separate from ``make_neo4j_driver`` so a ReBAC bind
    failure degrades cross-org admission to OFF without taking retrieval down."""
    if not settings.neo4j_uri:
        raise Neo4jUnconfiguredError("KRS_NEO4J_URI is not set")
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    await driver.verify_connectivity()
    return driver
