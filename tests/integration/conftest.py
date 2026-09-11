from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tabelshchik.adapters.db.engine import (
    create_all,
    create_db_engine,
    create_session_factory,
    session_scope,
)
from tabelshchik.adapters.db.seed import seed_offices
from tabelshchik.config.loader import load, parse_mapping
from tabelshchik.config.models import OfficeSeed

NOW = datetime(2026, 9, 11, 12, 0)
ANCHOR = date(2026, 9, 14)  # a Monday


@pytest.fixture
def sessions() -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(":memory:")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture
def real_config():
    return load(Path("config"))


def office_seed(**overrides) -> OfficeSeed:
    payload = {
        "id": "ovest",
        "name": "O'Vest",
        "chatId": -100123,
        "employees": [
            {"id": "anya", "name": "Аня", "telegramUsername": "anya_tg"},
            {"id": "borya", "name": "Боря"},
            {"id": "vera", "name": "Вера"},
            {"id": "gleb", "name": "Глеб"},
        ],
        "schedule": {"vacantDesks": {"monday": 2, "wednesday": 2}},
        **overrides,
    }
    return parse_mapping(payload, OfficeSeed, source="test.yaml")


def seed(sessions: sessionmaker[Session], *seeds: OfficeSeed, replace: bool = False):
    with session_scope(sessions) as session:
        return seed_offices(
            session, list(seeds) or [office_seed()], now=NOW, replace_existing=replace
        )
