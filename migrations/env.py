"""Alembic environment for MariaDB production and SQLite schema tests."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from retailprintguard.db import Base, models

del models  # importing registers every mapped table on Base.metadata

config = context.config
if config.config_file_name is not None:
    # Alembic may be invoked inside a long-lived worker or a test process that
    # already owns the application logging pipeline.  The stdlib default would
    # permanently disable every logger created before this call, including the
    # non-blocking proxy logger.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if database_url := os.environ.get("RPG_DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=url.startswith("sqlite"),
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
        if connection.dialect.name in {"mysql", "mariadb"}:
            connection.execute(text("SET time_zone = '+00:00'"))
            # SQLAlchemy 2 autobegins on SET.  If that transaction remains
            # open, Alembic does not own/commit it and MariaDB persists the DDL
            # while rolling back the alembic_version row when the connection
            # closes.  Commit the session setup before Alembic starts its own
            # migration transaction.
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
        if connection.dialect.name in {"mysql", "mariadb"}:
            # MariaDB DDL is non-transactional. Alembic's logical transaction
            # is therefore a no-op, while the final alembic_version INSERT is
            # ordinary transactional DML. Commit that revision marker
            # explicitly after every successful migration run.
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
