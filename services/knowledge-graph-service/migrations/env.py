"""Alembic environment for knowledge-graph-service.

Reads the DB URL from Settings (KGS_DATABASE_URL), coerced to the sync DSN via
`Settings.sync_database_url`, and targets `repositories.models.Base.metadata` so autogenerate and
`upgrade head` both see the canonical ORM schema. Sync engine (psycopg) is used for migrations even
though the app runs async (asyncpg) — standard Alembic practice.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from oraclous_knowledge_graph_service.core.config import get_settings
from oraclous_knowledge_graph_service.repositories.models import Base
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().sync_database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
