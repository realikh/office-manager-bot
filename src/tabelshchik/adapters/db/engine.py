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

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from tabelshchik.adapters.db.models import Base


def create_db_engine(database_path: Path | str, *, echo: bool = False) -> Engine:
    """Build an engine with the pragmas SQLite needs to behave itself."""
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{database_path}"
    else:
        url = "sqlite://"

    engine = create_engine(url, echo=echo, future=True)

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
    """Create the schema directly. Used by tests and by first-run bootstrap; production
    upgrades go through Alembic."""
    Base.metadata.create_all(engine)


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
