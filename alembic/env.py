"""Alembic environment.

The database URL is not written in alembic.ini. It comes from the same
``DATABASE_URL`` the application reads, so a migration can never be run
against a different database from the one the app is using -- which is how a
schema and an application quietly drift apart.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bookstore.db.base import Base, database_url   # noqa: E402
from bookstore.db import models                    # noqa: E402,F401

config = context.config
config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without this, changing a column's type is silently not detected
            # and autogenerate produces an empty migration.
            compare_type=True,
            # SQLite cannot ALTER most things in place. Batch mode rebuilds
            # the table instead, so the same migration runs on both backends.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
