# oraclous-knowledge-retriever-service

Substrate layer (rebuilt real in R3.5). A thin, **read-only** retrieval service over the
knowledge graph: semantic search, subgraph retrieval, federated read, and a retrieval-eval
endpoint. It reads the graph and a Redis query cache; it owns no relational storage and writes
no graph data.

## §21 structure note (read-only deviation, #301)

This service intentionally omits two of the canonical layers, and that absence is **recorded as
accepted**, not an unfinished gap:

- **no `domain/`** — there are no business-logic aggregates; a request is *parse query → read
  substrate → shape response*. §21 makes `domain/` optional ("only if the service has domain
  rules").
- **no `models/`** — it owns no relational schema (no ORM `__tablename__`). Its only persistence
  is a Redis query cache, which is repository-layer access, not ORM declarations.

The deviation is registered in `tools/lint/service_status.yaml` under this service's
`structure_exceptions`, and `tools/lint/check_service_structure.py` surfaces it as an accepted
exception (and re-flags it as STR006 if either layer is ever added, so the note can't go stale).
See `oraclous-knowledge/engineering/service-architecture-standard.md` for the §21 narrative.
