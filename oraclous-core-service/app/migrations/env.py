import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# Ensure the oraclous-core-service/ directory is on sys.path so that
# app.models.* imports work the same way they do when running pytest.
_migrations_dir = Path(__file__).parent
_app_dir = _migrations_dir.parent
_core_service_dir = _app_dir.parent
if str(_core_service_dir) not in sys.path:
    sys.path.insert(0, str(_core_service_dir))

config = context.config

# Pick up -x sqlalchemy.url=... override passed by the test conftest.
_x_args = context.get_x_argument(as_dictionary=True)
if "sqlalchemy.url" in _x_args:
    config.set_main_option("sqlalchemy.url", _x_args["sqlalchemy.url"])

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# target_metadata is None because all migrations are written by hand (no autogenerate).
target_metadata = None


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    url = config.get_main_option("sqlalchemy.url")
    engine = create_async_engine(url, poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online():
    asyncio.run(run_async_migrations())


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
