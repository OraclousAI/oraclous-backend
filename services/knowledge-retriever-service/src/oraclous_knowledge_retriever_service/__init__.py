"""oraclous-knowledge-retriever-service — semantic search + subgraph retrieval (substrate).

§21 note (#301): this is a THIN READ-ONLY service. It intentionally has no `domain/` layer
(no business-logic aggregates — it parses a query and reads the graph/query-cache) and no
`models/` layer (it owns no relational schema; its only persistence is a Redis query cache,
which lives in `repositories/`). §21 makes `domain/` optional; the deviation is recorded as
accepted in `tools/lint/service_status.yaml` (`structure_exceptions`).
"""
