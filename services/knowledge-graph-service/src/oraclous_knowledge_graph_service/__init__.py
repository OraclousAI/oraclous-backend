"""oraclous-knowledge-graph-service — concern-driven ingestion → graph (substrate).

§21 note (#302): this service is graph-primary (Neo4j). Its relational ORM table declarations
(knowledge_graphs, recipes, entity_resolutions, ingestion_jobs) are centralised in
`repositories/models.py` — the colocated form §21/STR004 treats as equivalent to a sibling
`models/` package — so there is no separate `models/` layer (recorded in
`tools/lint/service_status.yaml` `structure_exceptions`).
"""
