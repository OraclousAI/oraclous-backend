"""Alembic environment for auth-service.

Reads the DB URL from Settings (``DATABASE_URL``), coerced to the sync DSN via
``Settings.sync_database_url``, and targets ``oraclous_auth_service.models.Base.metadata`` —
importing the models package registers every table (agents, agent_credentials, users, refresh).
A sync engine (psycopg) runs migrations though the app is async (asyncpg) — standard Alembic.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from oraclous_auth_service.core.config import get_settings
from oraclous_auth_service.models import Base
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().sync_database_url)
target_metadata = Base.metadata

# The dev stack shares one Postgres database across services (KGS, auth, …), each with its OWN
# migration lineage. A dedicated version table keeps auth-service's Alembic history independent of
# KGS's default ``alembic_version`` (otherwise auth would read KGS's head and fail to locate it).
_VERSION_TABLE = "alembic_version_auth"


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        version_table=_VERSION_TABLE,
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            version_table=_VERSION_TABLE,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
