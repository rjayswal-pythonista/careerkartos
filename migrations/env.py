"""Alembic environment.

Scope, deliberately narrow: Alembic owns *changes* to the schema. Creating
tables in the first place stays with Base.metadata.create_all() in app/db.py.

That split is unusual, so the reasoning matters. Production already has a
populated schema built by create_all, and the two suites run against throwaway
SQLite files. Handing full ownership to Alembic would mean re-baselining a live
database and making both suites migrate before every run — real risk and real
slowdown, to buy a property (reproducible fresh environments) that create_all
already provides.

What was actually missing is the ability to ship a change that is not an
ADD COLUMN, in a defined order, as a deploy step rather than at application
startup. Startup DDL cannot survive more than one instance: every booting
process issues the same ALTER concurrently. That is the gap this closes.

    new table              -> add the model; create_all picks it up
    change existing column -> write a migration here
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.schema import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The URL comes from the environment, never from alembic.ini — the ini is
# committed to the repository and a connection string is a credential.
config.set_main_option(
    "sqlalchemy.url",
    os.environ.get("DATABASE_URL", "sqlite:///./jobs.db"),
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead. Local development and both suites use SQLite.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
