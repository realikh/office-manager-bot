"""Alembic environment.

The database URL comes from the same place the bot gets it, so a migration can never be
run against a different database than the one the bot is about to open.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

from tabelshchik.adapters.db.models import Base
from tabelshchik.bootstrap.settings import database_path

config = context.config

# Only configure logging when Alembic owns the process. Running from the bot's own boot
# path, the application has already set logging up and this would undo it.
if config.config_file_name is not None and "connection" not in config.attributes:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    configured = config.get_main_option("sqlalchemy.url", "")
    if configured:
        return configured

    target = database_path()
    # The application's engine factory creates this directory; Alembic builds its own
    # engine and would not. On a fresh checkout — CI, or a clone on a new machine —
    # `data/` does not exist and SQLite reports only "unable to open database file".
    target.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{target}"


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things in place, so Alembic rebuilds the table and
        # copies the data across. Without this, almost every schema change fails.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    existing = config.attributes.get("connection")
    if existing is not None:
        # Handed a live connection by the application's boot path. Use it directly:
        # calling .connect() on a Connection is not the same as on an Engine, and
        # getting that wrong makes the upgrade silently do nothing.
        _run(existing)
        return

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as connection:
        _run(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
