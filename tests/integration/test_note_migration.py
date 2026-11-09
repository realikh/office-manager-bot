from __future__ import annotations

from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text

from tabelshchik.adapters.db.engine import create_db_engine

ROOT = Path(__file__).resolve().parents[2]
BEFORE_NOTES = "47b7acff4a10"


def migrate(engine: Engine, revision: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def test_remembered_facts_survive_the_move_to_notes(tmp_path: Path) -> None:
    engine = create_db_engine(tmp_path / "bot.db")
    migrate(engine, BEFORE_NOTES)
    at = datetime(2026, 10, 1, 9, 30).isoformat(sep=" ")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO office (id, name, address, timezone, holiday_calendar,"
                " seed_nonce, active) VALUES ('ovest', 'O''Vest', '', 'Asia/Almaty', 'KZ', 0, 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO employee (id, office_id, full_name, telegram_user_id, gender)"
                " VALUES ('anya', 'ovest', 'Аня', 777, 'female'),"
                " ('borya', 'ovest', 'Боря', NULL, 'male')"
            )
        )
        connection.execute(
            text("INSERT INTO chat_memory (scope, subject, fact, at) VALUES (:s, :j, :f, :a)"),
            [
                {"s": "office", "j": "ovest", "f": "Стендап в 10:30", "a": at},
                {"s": "employee", "j": "anya", "f": "Пьет кофе без сахара", "a": at},
                {"s": "employee", "j": "borya", "f": "Работает из Алматы", "a": at},
            ],
        )

    migrate(engine, "head")

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT scope, subject, topic, text FROM memory_note ORDER BY scope, subject")
        ).all()
    assert [tuple(row) for row in rows] == [
        ("office", "ovest", "стендап в 10 30", "Стендап в 10:30"),
        ("person", "777", "пьет кофе без сахара", "Пьет кофе без сахара"),
    ]
    assert "chat_memory" not in inspect(engine).get_table_names()
