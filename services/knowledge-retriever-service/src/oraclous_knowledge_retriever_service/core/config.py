"""Service configuration (ORAA-4 §21 core layer) — env → Settings (KRS read side).

KRS is read-only: it queries the org-scoped Neo4j graph that knowledge-graph-service writes. Same
dev-auth seam + same dev organisation as KGS (so it reads the data KGS wrote), and the SAME
deterministic hashing embedder + dimension (512) so a query vector lives in the same space as the
stored chunk embeddings — key-free semantic search. `neo4j_uri` has no hardcoded default (ORAA-53).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KRS_", extra="ignore")

    # --- identity seam. `gateway` (production, ADR-018): trust the gateway's verified
    # X-Principal-*/X-Organisation-Id headers, gated by X-Internal-Key — no token validation here.
    # `dev`: a fixed bearer for the standalone smoke. `jwt`: decode a real auth-service token. ---
    auth_mode: Literal["gateway", "dev", "jwt"] = "dev"
    dev_bearer: str = "dev-token"
    dev_user_id: str = "00000000-0000-0000-0000-0000000000d5"
    dev_org_id: str = "00000000-0000-0000-0000-00000000050a"
    # gateway mode: the shared secret the gateway sends as X-Internal-Key; fail-closed if unset.
    internal_service_key: str | None = None
    # jwt mode: the shared HS256 secret the auth-service signs with (compose injects KRS_JWT_SECRET
    # = the auth-service JWT_SECRET). No default — jwt mode fails closed without it.
    jwt_secret: str | None = None
    jwt_algorithm: str = "HS256"

    # --- Neo4j (read role krs_reader, ORAA-53). No hardcoded URI default. ---
    neo4j_uri: str | None = None
    neo4j_user: str = "krs_reader"
    neo4j_password: str = "krs-reader-pass"  # noqa: S105 — dev default; prod injects via secret
    neo4j_database: str | None = None

    # --- retrieval embedder (MUST match the KGS write-side hashing embedder for convergence) ---
    embedding_dim: int = 512
    default_top_k: int = 10

    # --- federated cross-graph reads (#330 / ADR-026). The accessible-set is enumerated from the
    # KGS Postgres graph registry over the internal plane (GET /internal/v1/graphs, X-Internal-Key)
    # — KRS has no registry DB access and may not import the sibling service, so the internal
    # endpoint is the seam. Unset ⇒ federated endpoints fail closed (503): no enumeration, no
    # fan-out, never "assume all". Caps are config (ADR-026): the most graphs one query fans out
    # over, the most hits one graph may contribute, and the merged total cap. ---
    knowledge_graph_url: str | None = None
    federated_max_graphs: int = 20
    federated_max_per_graph_k: int = 25
    federated_max_total: int = 200

    # --- Redis query cache (#308, lift-and-reshape of legacy query_cache_service). Advisory: a
    # Redis outage degrades to a live query, never an error. OFF by default — opt-in via
    # KRS_QUERY_CACHE=true so the standalone/no-Redis run is unaffected. The cache key folds in a
    # per-graph generation counter the KGS ingest bumps (a neutral "graph version" signal, NOT the
    # retriever's private key layout), so a fresh ingest is a natural cache-miss with no cross-
    # service key-format coupling. Same redis_url as the KGS ingestion spine. ---
    query_cache: bool = False
    query_cache_ttl: int = 300  # seconds a cached read survives absent a generation bump (5 min)
    redis_url: str = "redis://redis:6379/0"

    # --- retrieval-quality evaluation (#331): the LLM judge. ONE OpenAI-compatible client
    # (mirrors KGS_OPENAI_* — OpenRouter by default), built ONCE at lifespan. No key → the
    # /evaluate endpoint returns a typed 422; it NEVER fabricates scores. The caps bound judge
    # spend per request AND per process; the deadline keeps every response under the gateway's
    # 30s read timeout (#333). ---
    openai_api_key: str | None = None
    openai_base_url: str = "https://openrouter.ai/api/v1"
    eval_judge_model: str = "openai/gpt-4o-mini"
    eval_judge_timeout_seconds: float = 15.0  # per-call HTTP timeout on the judge client
    eval_judge_max_retries: int = 1  # SDK retry attempts (default 2 would burn the deadline)
    eval_judge_max_tokens: int = 2000  # JSON judging output cap (decomposition needs headroom)
    eval_deadline_seconds: float = 25.0  # whole-evaluation deadline — UNDER the gateway's 30s
    eval_top_k: int = 5  # retrieved contexts (existing hybrid path) the metrics judge against
    eval_max_concurrency: int = 5  # in-flight judge calls (asyncio.Semaphore, the #272 pattern)
    eval_max_concurrent_requests: int = 4  # process-level cap on evaluations in flight (429 over)
    eval_max_claims: int = 25  # cap on answer claims / ground-truth statements judged per call
    eval_max_contexts: int = 5  # judged-context cap, applied ONCE so all metrics see one set
    eval_grounded_threshold: float = 0.7  # faithfulness >= this -> is_grounded


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
