"""Neo4j connection (ORAA-4 §21 core layer — connection setup).

A single sync `neo4j.Driver` opened as the read role (ORAA-53) from `KRS_NEO4J_*`. KRS is strictly
READ-ONLY (ORAA-58 / T6): it never issues write Cypher and never creates indexes — schema (any
fulltext index included) is owned by the write side (knowledge-graph-service). The fulltext modality
therefore uses an index-free `CONTAINS` scan, org-scoped in-query (Community lacks RLS).
"""

from __future__ import annotations

from neo4j import Driver, GraphDatabase

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
