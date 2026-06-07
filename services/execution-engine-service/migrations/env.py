"""Alembic environment for execution-engine-service.

Reads the DB URL from Settings (``DATABASE_URL``, coerced to the sync DSN) and targets
``oraclous_execution_engine_service.models.Base.metadata`` (importing the models package registers
``engine_jobs`` + ``engine_provenance``). Uses its OWN ``version_table`` — the dev stack shares one
Postgres across services, each with an independent migration lineage.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from oraclous_execution_engine_service.core.config import get_settings
from oraclous_execution_engine_service.models import Base
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().sync_database_url)
target_metadata = Base.metadata

_VERSION_TABLE = "alembic_version_execution_engine"


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
