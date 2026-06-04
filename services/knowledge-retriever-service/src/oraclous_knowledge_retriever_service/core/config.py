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

    # --- identity seam (dev-auth / single-tenant; same dev org as KGS) ---
    auth_mode: Literal["dev", "jwt"] = "dev"
    dev_bearer: str = "dev-token"
    dev_user_id: str = "00000000-0000-0000-0000-0000000000d5"
    dev_org_id: str = "00000000-0000-0000-0000-00000000050a"

    # --- Neo4j (read role krs_reader, ORAA-53). No hardcoded URI default. ---
    neo4j_uri: str | None = None
    neo4j_user: str = "krs_reader"
    neo4j_password: str = "krs-reader-pass"  # noqa: S105 — dev default; prod injects via secret
    neo4j_database: str | None = None

    # --- retrieval embedder (MUST match the KGS write-side hashing embedder for convergence) ---
    embedding_dim: int = 512
    default_top_k: int = 10


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
