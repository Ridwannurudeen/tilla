"""Alembic environment for Tilla.

The URL is derived from ``app.config.DB_PATH`` (env ``TILLA_DB_PATH``, default
``/opt/tilla/tilla.db``) so the same source of truth drives the app and every
migration, even when run over SSH outside the systemd EnvironmentFile.
``render_as_batch=True`` routes all future SQLite ALTERs through batch mode;
``compare_type=True`` so autogenerate notices type changes.
"""

from logging.config import fileConfig

from sqlalchemy import create_engine

import app.models  # noqa: F401 — imported for the side effect of registering tables
from alembic import context
from app import config
from app.db import Base

alembic_config = context.config
if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

target_metadata = Base.metadata
DB_URL = f"sqlite:///{config.DB_PATH}"


def run_migrations_offline() -> None:
    context.configure(
        url=DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(DB_URL)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
