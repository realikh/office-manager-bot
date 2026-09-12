"""Hiring and firing from inside the bot.

The interesting cases are all about what a change does to everything *else*: the schedule
somebody was already on, the fairness ledger, and the admin's ability to undo a misclick.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.repositories import (
    SqlLedgerStore,
    SqlOfficeStore,
    SqlRosterStore,
    SqlScheduleStore,
)
from tabelshchik.application import manage_roster
from tabelshchik.application.ids import MAX_ID_LENGTH, slugify, unique_id
from tabelshchik.application.manage_roster import RosterError
from tabelshchik.application.policy import SchedulePolicy
from tabelshchik.application.regenerate_schedule import regenerate

from .conftest import ANCHOR, office_seed, seed

TODAY = date(2026, 9, 11)
NOON = datetime(2026, 9, 11, 12, 0)
POLICY = SchedulePolicy(horizon_weeks=4, freeze_weeks=1)


@pytest.fixture
def office(sessions):
    seed(sessions)
    regenerate(
        office_id="ovest",
        offices=SqlOfficeStore(sessions),
        schedule=SqlScheduleStore(sessions),
        ledger=SqlLedgerStore(sessions),
        clock=FixedClock(NOON),
        policy=POLICY,
        triggered_by="test",
    )
    return sessions


def stores(sessions, now: datetime = NOON):
    return {
        "offices": SqlOfficeStore(sessions),
        "roster": SqlRosterStore(sessions),
        "schedule": SqlScheduleStore(sessions),
        "ledger": SqlLedgerStore(sessions),
        "clock": FixedClock(now),
        "policy": POLICY,
    }


def roster_ids(sessions) -> set[str]:
    return {employee.id for employee in SqlOfficeStore(sessions).employees("ovest")}


def scheduled_days(sessions, employee_id: str, *, frm: date = TODAY) -> list[date]:
    return [
        snapshot.day
        for snapshot in SqlScheduleStore(sessions).days_between(
            "ovest", frm, frm + timedelta(days=90)
        )
        if employee_id in snapshot.roster
    ]


# --------------------------------------------------------------------------- slugs


def test_a_latin_name_becomes_the_id_the_seeds_already_use() -> None:
    assert slugify("Alikhan Khassen") == "alikhan-khassen"


def test_a_cyrillic_name_is_transliterated_rather_than_dropped() -> None:
    """A naive slugifier returns an empty string here, and an empty id fails the
    identifier pattern at the next config validation."""
    assert slugify("Жания Жакипова") == "zhaniya-zhakipova"
    assert slugify("Мухидин Нуридинов") == "mukhidin-nuridinov"


def test_punctuation_and_spacing_collapse() -> None:
    assert slugify("  O'Neill,  Seán  ") == "o-neill-sean"


def test_an_id_is_never_longer_than_a_callback_can_carry() -> None:
    assert len(slugify("Константин" * 10)) <= MAX_ID_LENGTH


def test_a_collision_gets_a_number_not_an_integrity_error() -> None:
    """`SqlRosterStore.add_employee` does no duplicate check; a clash there surfaces as an
    IntegrityError out of a session scope, which the admin never sees."""
    assert unique_id("Ivan Petrov", {"ivan-petrov"}) == "ivan-petrov-2"
    assert unique_id("Ivan Petrov", {"ivan-petrov", "ivan-petrov-2"}) == "ivan-petrov-3"


def test_a_name_with_nothing_transliterable_still_yields_a_valid_id() -> None:
    assert unique_id("字", set()) == "employee"


# ----------------------------------------------------------------------------- add


def test_a_new_hire_is_scheduled_without_waiting_for_the_weekly_run(office) -> None:
    change = manage_roster.add_employee(
        office_id="ovest", full_name="Дина Ким", gender="female", actor_id=1, **stores(office)
    )

    assert change.employee_id == "dina-kim"
    assert "dina-kim" in roster_ids(office)
    assert scheduled_days(office, "dina-kim"), "a new hire with no days looks like a bug"


def test_a_new_hire_starts_today_so_earlier_days_are_not_theirs(office) -> None:
    manage_roster.add_employee(
        office_id="ovest", full_name="Дина Ким", actor_id=1, **stores(office)
    )
    employee = next(e for e in SqlOfficeStore(office).employees("ovest") if e.id == "dina-kim")
    assert employee.started_on == TODAY
    assert not employee.in_tenure(TODAY - timedelta(days=1))


def test_a_duplicate_name_gets_its_own_id(office) -> None:
    first = manage_roster.add_employee(
        office_id="ovest", full_name="Аня", actor_id=1, **stores(office)
    )
    assert first.employee_id != "anya"
    assert {"anya", first.employee_id} <= roster_ids(office)


def test_an_empty_name_is_refused(office) -> None:
    with pytest.raises(RosterError):
        manage_roster.add_employee(office_id="ovest", full_name="   ", actor_id=1, **stores(office))


def test_a_malformed_username_is_refused_before_anything_is_written(office) -> None:
    with pytest.raises(RosterError):
        manage_roster.add_employee(
            office_id="ovest", full_name="Дина Ким", username="ой!", actor_id=1, **stores(office)
        )
    assert "dina-kim" not in roster_ids(office)


def test_a_username_someone_else_already_has_is_refused(office) -> None:
    with pytest.raises(RosterError):
        manage_roster.add_employee(
            office_id="ovest",
            full_name="Дина Ким",
            username="anya_tg",
            actor_id=1,
            **stores(office),
        )


def test_a_leading_at_sign_is_accepted_and_stripped(office) -> None:
    manage_roster.add_employee(
        office_id="ovest",
        full_name="Дина Ким",
        username="@dina_kim",
        actor_id=1,
        **stores(office),
    )
    employee = next(e for e in SqlOfficeStore(office).employees("ovest") if e.id == "dina-kim")
    assert employee.telegram_username == "dina_kim"


# ------------------------------------------------------------------- ending a tenure


def test_ending_a_tenure_frees_the_days_and_fills_them_again(office) -> None:
    before = scheduled_days(office, "borya")
    assert before, "the fixture has to give them days for this to mean anything"

    change = manage_roster.end_tenure(
        office_id="ovest", employee_id="borya", actor_id=1, **stores(office)
    )

    assert change.released
    assert not [day for day in scheduled_days(office, "borya") if day > TODAY]
    # The desks did not simply vanish with them.
    for day in change.released:
        assert SqlScheduleStore(office).day("ovest", day).roster


def test_the_person_and_their_history_survive_the_removal(office) -> None:
    """Soft on purpose. A hard delete cascades away every assignment they ever had and
    silently rewrites everyone else's fairness numbers."""
    ledger_before = SqlLedgerStore(office).entries("ovest")

    manage_roster.end_tenure(office_id="ovest", employee_id="borya", actor_id=1, **stores(office))

    employee = next(e for e in SqlOfficeStore(office).employees("ovest") if e.id == "borya")
    assert employee.ended_on == TODAY
    assert not employee.in_tenure(TODAY + timedelta(days=1))
    assert "borya" in SqlLedgerStore(office).entries("ovest") or not ledger_before


def test_a_removal_is_undoable(office) -> None:
    manage_roster.end_tenure(office_id="ovest", employee_id="borya", actor_id=1, **stores(office))
    manage_roster.restore(office_id="ovest", employee_id="borya", actor_id=1, **stores(office))

    employee = next(e for e in SqlOfficeStore(office).employees("ovest") if e.id == "borya")
    assert employee.ended_on is None
    assert scheduled_days(office, "borya")


def test_removing_somebody_from_another_office_is_refused(office) -> None:
    seed(office, office_seed(id="pine", name="Pine", chatId=-100999, employees=[]))
    with pytest.raises(RosterError):
        manage_roster.end_tenure(
            office_id="pine", employee_id="borya", actor_id=1, **stores(office)
        )


# ----------------------------------------------------------------------- edits


def test_renaming_keeps_the_id_so_the_schedule_still_points_at_them(office) -> None:
    days = scheduled_days(office, "borya")

    manage_roster.rename(
        office_id="ovest",
        employee_id="borya",
        full_name="Борис Борисов",
        actor_id=1,
        offices=SqlOfficeStore(office),
        roster=SqlRosterStore(office),
    )

    employee = next(e for e in SqlOfficeStore(office).employees("ovest") if e.id == "borya")
    assert employee.full_name == "Борис Борисов"
    assert scheduled_days(office, "borya") == days


def test_a_username_can_be_cleared(office) -> None:
    manage_roster.set_username(
        office_id="ovest",
        employee_id="anya",
        username=None,
        actor_id=1,
        offices=SqlOfficeStore(office),
        roster=SqlRosterStore(office),
    )
    employee = next(e for e in SqlOfficeStore(office).employees("ovest") if e.id == "anya")
    assert employee.telegram_username is None


def test_taking_a_username_someone_else_holds_is_refused(office) -> None:
    with pytest.raises(RosterError):
        manage_roster.set_username(
            office_id="ovest",
            employee_id="borya",
            username="anya_tg",
            actor_id=1,
            offices=SqlOfficeStore(office),
            roster=SqlRosterStore(office),
        )


def test_keeping_your_own_username_is_not_a_collision(office) -> None:
    manage_roster.set_username(
        office_id="ovest",
        employee_id="anya",
        username="anya_tg",
        actor_id=1,
        offices=SqlOfficeStore(office),
        roster=SqlRosterStore(office),
    )
    employee = next(e for e in SqlOfficeStore(office).employees("ovest") if e.id == "anya")
    assert employee.telegram_username == "anya_tg"


def test_the_anchor_fixture_is_still_a_monday() -> None:
    assert ANCHOR.weekday() == 0
