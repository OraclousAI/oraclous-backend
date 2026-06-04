"""Seed the dev organisation with a starter graph (idempotent).

The knowledge-graph-service owns no organisation/user tables — those belong to the identity/org
service (R3.5-P3); the dev org/user are config-provided IDs used only for scoping. This script
therefore seeds one demo `knowledge_graphs` row for the dev org so a fresh stack has something to
list, and doubles as a DB-connectivity check for the `kgs-seed` one-shot. Safe to run repeatedly.

Run:  python services/knowledge-graph-service/scripts/seed_dev.py
"""

from __future__ import annotations

import uuid

from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.repositories.models import KnowledgeGraph
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

_DEMO_NAME = "dev-demo-graph"


def main() -> None:
    settings = get_settings()
    org_id = uuid.UUID(settings.dev_org_id)
    user_id = uuid.UUID(settings.dev_user_id)
    engine = create_engine(settings.sync_database_url, future=True)
    with Session(engine) as session:
        exists = session.execute(
            select(KnowledgeGraph.id).where(
                KnowledgeGraph.organisation_id == org_id,
                KnowledgeGraph.name == _DEMO_NAME,
            )
        ).first()
        if exists is not None:
            print(f"seed_dev: '{_DEMO_NAME}' already present for dev org — nothing to do")
            return
        session.add(
            KnowledgeGraph(
                id=uuid.uuid4(),
                organisation_id=org_id,
                user_id=user_id,
                name=_DEMO_NAME,
                description="Starter graph seeded for the dev organisation.",
            )
        )
        session.commit()
        print(f"seed_dev: created '{_DEMO_NAME}' for dev org {org_id}")
    engine.dispose()


if __name__ == "__main__":
    main()
