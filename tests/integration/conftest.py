from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tabelshchik.adapters.db.engine import (
    create_all,
    create_db_engine,
    create_session_factory,
    session_scope,
)
from tabelshchik.adapters.db.seed import seed_offices
from tabelshchik.config.loader import parse_mapping
from tabelshchik.config.models import OfficeSeed

NOW = datetime(2026, 9, 11, 12, 0)
ANCHOR = date(2026, 9, 14)  # a Monday


@pytest.fixture
def sessions() -> Iterator[sessionmaker[Session]]:
    engine = create_db_engine(":memory:")
    create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


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


def big_office_seed(**overrides) -> OfficeSeed:
    """Twelve people against eleven Friday desks — the dense case.

    The shipped office files used to stand in for this, and a four-person toy office
    proves much less: it is the tight ratio that makes `shortfall == 0` mean anything.
    """
    payload = {
        "id": "ovest",
        "name": "O'Vest",
        "chatId": -100123,
        "employees": [{"id": f"p{index}", "name": f"Сотрудник {index}"} for index in range(12)],
        "schedule": {"vacantDesks": {"friday": 11}},
        **overrides,
    }
    return parse_mapping(payload, OfficeSeed, source="test.yaml")


def fixed_office_seed(**overrides) -> OfficeSeed:
    """Fixed-schedule only: nothing to draft, so the solver short-circuits."""
    payload = {
        "id": "pine-office-park",
        "name": "Pine Office Park",
        "chatId": -100123,
        "employees": [{"id": f"q{index}", "name": f"Коллега {index}"} for index in range(6)],
        "schedule": {"fixed": {"monday": ["q0", "q1"], "friday": ["q2", "q3"]}},
        **overrides,
    }
    return parse_mapping(payload, OfficeSeed, source="test.yaml")


def seed(sessions: sessionmaker[Session], *seeds: OfficeSeed, replace: bool = False):
    with session_scope(sessions) as session:
        return seed_offices(
            session, list(seeds) or [office_seed()], now=NOW, replace_existing=replace
        )
