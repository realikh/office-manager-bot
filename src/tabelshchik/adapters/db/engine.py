"""Database engine and sessions.

Deliberately synchronous. The bot is async, but this is a local SQLite file serving a few
dozen people: queries are sub-millisecond, and an async driver would add a dependency and
a class of bugs in exchange for latency nobody can perceive. Genuinely slow work — a
schedule solve, a workbook build — is handed to ``asyncio.to_thread`` at the call site
instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from tabelshchik.adapters.db.models import Base


def create_db_engine(database_path: Path | str, *, echo: bool = False) -> Engine:
    """Build an engine with the pragmas SQLite needs to behave itself."""
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{database_path}"
        # NullPool: every session opens and closes its own connection. Transactions here
        # are sub-millisecond, so pooling buys nothing, while idle pooled handles cost
        # non-deterministic cleanup and the occasional "database is locked".
        kwargs: dict[str, Any] = {"poolclass": NullPool}
    else:
        url = "sqlite://"
        # An in-memory database lives only as long as its connection, so the one
        # connection has to be shared or every session would see an empty schema.
        kwargs = {
            "poolclass": StaticPool,
            "connect_args": {"check_same_thread": False},
        }

    engine = create_engine(url, echo=echo, future=True, **kwargs)

    @event.listens_for(engine, "connect")
    def _configure(connection, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = connection.cursor()
        # Off by default in SQLite, which would make every ondelete="CASCADE" a lie.
        cursor.execute("PRAGMA foreign_keys=ON")
        # Survives an unclean shutdown without losing the last writes.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine: Engine) -> None:
    """Create the schema directly, without a migration history.

    For tests and in-memory databases only. A real database goes through
    :func:`upgrade_schema`, so that changing a model later is a migration rather than a
    manual rescue of live data.
    """
    Base.metadata.create_all(engine)


def upgrade_schema(engine: Engine) -> None:
    """Bring the database up to the latest migration.

    Runs on every boot. On a fresh database this creates everything; on an existing one
    it applies whatever is outstanding and is a no-op when there is nothing to do.
    """
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[3].parent
    ini = root / "alembic.ini"
    if not ini.is_file():
        # Running from an installed wheel without the migration tree beside it; fall
        # back rather than refusing to start.
        Base.metadata.create_all(engine)
        return

    config = Config(str(ini))
    config.set_main_option("script_location", str(root / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """A transaction that commits on success and rolls back on anything else."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
