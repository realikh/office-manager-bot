"""Creating, editing and retiring offices.

An office used to be a YAML file that was read once and skipped forever. These pin what
replaced it — in particular that closing an office actually stops the things that speak,
which per-office jobs registered at boot do not do on their own.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from tabelshchik.adapters.clock import FixedClock
from tabelshchik.adapters.db.engine import session_scope
from tabelshchik.adapters.db.repositories import (
    SqlAdminStore,
    SqlAuditLog,
    SqlOfficeAdminStore,
    SqlOfficeStore,
    SqlRosterStore,
    SqlScheduleStore,
)
from tabelshchik.adapters.db.seed import seed_admins
from tabelshchik.adapters.fakes import RecordingNotifier
from tabelshchik.application import manage_offices
from tabelshchik.application.audience import Audience, resolve
from tabelshchik.application.ids import MAX_OFFICE_ID_LENGTH
from tabelshchik.application.manage_offices import OfficeError

from .conftest import seed

NOW = datetime(2026, 9, 11, 12, 0)
TODAY = date(2026, 9, 11)
OWNER = 1
ADMIN = 2


@pytest.fixture
def stores(sessions):
    seed(sessions)
    with session_scope(sessions) as session:
        seed_admins(session, frozenset({OWNER}), now=NOW)
    admins = SqlAdminStore(sessions)
    admins.grant(ADMIN, at=NOW)
    return {
        "offices": SqlOfficeStore(sessions),
        "office_admin": SqlOfficeAdminStore(sessions),
        "roster": SqlRosterStore(sessions),
        "admins": admins,
        "audit": SqlAuditLog(sessions),
    }


def owner_stores(stores: dict) -> dict:
    """What the lifecycle functions take, minus the roster they do not use."""
    return {k: v for k, v in stores.items() if k != "roster"}


# ------------------------------------------------------------------------------ create


def test_a_new_office_gets_a_readable_id(stores) -> None:
    change = manage_offices.create_office(
        name="Алматы Тауэр", actor_id=OWNER, **owner_stores(stores)
    )
    assert change.office_id == "almaty-tauer"
    assert stores["offices"].get_office("almaty-tauer") is not None


def test_a_new_office_id_fits_a_callback(stores) -> None:
    """Office ids ride alongside another id inside 64 bytes of callback data."""
    change = manage_offices.create_office(
        name="Очень Длинное Название Офиса Которое Никто Не Сократил",
        actor_id=OWNER,
        **owner_stores(stores),
    )
    assert len(change.office_id) <= MAX_OFFICE_ID_LENGTH


def test_a_colliding_name_gets_a_number(stores) -> None:
    first = manage_offices.create_office(name="Точка", actor_id=OWNER, **owner_stores(stores))
    second = manage_offices.create_office(name="Точка", actor_id=OWNER, **owner_stores(stores))
    assert first.office_id != second.office_id


def test_a_closed_office_still_holds_its_id(stores) -> None:
    """Handing the same id out twice is a primary-key violation out of a session scope,
    which the admin would see as nothing happening at all."""
    created = manage_offices.create_office(name="Точка", actor_id=OWNER, **owner_stores(stores))
    manage_offices.close_office(office_id=created.office_id, actor_id=OWNER, **owner_stores(stores))

    again = manage_offices.create_office(name="Точка", actor_id=OWNER, **owner_stores(stores))
    assert again.office_id != created.office_id


def test_an_empty_name_is_refused(stores) -> None:
    with pytest.raises(OfficeError, match="пустым"):
        manage_offices.create_office(name="   ", actor_id=OWNER, **owner_stores(stores))


def test_only_the_owner_may_create_an_office(stores) -> None:
    with pytest.raises(OfficeError, match="владелец"):
        manage_offices.create_office(name="Точка", actor_id=ADMIN, **owner_stores(stores))


def test_a_new_office_needs_no_schedule_of_its_own(sessions, stores) -> None:
    """It has nobody and no desks, so there is nothing to solve. The first hire
    regenerates, which `manage_roster.add_employee` already does."""
    created = manage_offices.create_office(name="Точка", actor_id=OWNER, **owner_stores(stores))
    assert not SqlScheduleStore(sessions).has_schedule_from(created.office_id, TODAY)


# -------------------------------------------------------------------------------- edit


def test_renaming_leaves_the_id_alone(stores) -> None:
    """The id is referenced by assignments, ledger entries and the weekly template. A
    rename is a change of label, not of person."""
    manage_offices.rename_office(
        office_id="ovest",
        name="О'Вест",
        actor_id=ADMIN,
        offices=stores["offices"],
        office_admin=stores["office_admin"],
        audit=stores["audit"],
    )
    office = stores["offices"].get_office("ovest")
    assert office is not None and office.name == "О'Вест"


def test_binding_a_private_chat_is_refused(stores) -> None:
    """A positive id is a DM. Binding an office to one posts that office's full tagged
    roster into a single person's messages every working day."""
    with pytest.raises(OfficeError, match="отрицательн"):
        manage_offices.bind_chat(
            office_id="ovest",
            chat_id=12345,
            actor_id=ADMIN,
            offices=stores["offices"],
            roster=stores["roster"],
            audit=stores["audit"],
        )


def test_two_offices_may_share_one_chat(stores) -> None:
    """Supported on purpose: messages into a shared chat carry an office header."""
    created = manage_offices.create_office(name="Точка", actor_id=OWNER, **owner_stores(stores))
    manage_offices.bind_chat(
        office_id=created.office_id,
        chat_id=-100123,
        actor_id=ADMIN,
        offices=stores["offices"],
        roster=stores["roster"],
        audit=stores["audit"],
    )
    office = stores["offices"].get_office(created.office_id)
    assert office is not None and office.chat_id == -100123


def test_an_unknown_holiday_calendar_is_refused(stores) -> None:
    """`country_holidays` returns an empty mapping for an unknown code, so a typo is a
    silent "no public holidays, ever"."""
    with pytest.raises(OfficeError, match="Код календаря"):
        manage_offices.set_holiday_calendar(
            office_id="ovest",
            code="Kazakhstan!",
            actor_id=ADMIN,
            offices=stores["offices"],
            office_admin=stores["office_admin"],
            audit=stores["audit"],
        )


# ---------------------------------------------------------------------- close and open


def test_closing_keeps_everything_and_reopens_in_one_step(stores) -> None:
    manage_offices.close_office(office_id="ovest", actor_id=OWNER, **owner_stores(stores))
    office = stores["offices"].get_office("ovest")
    assert office is not None and not office.active
    assert len(stores["offices"].employees("ovest")) == 4

    manage_offices.reopen_office(office_id="ovest", actor_id=OWNER, **owner_stores(stores))
    office = stores["offices"].get_office("ovest")
    assert office is not None and office.active


def test_a_closed_office_is_out_of_the_active_list_but_not_the_full_one(stores) -> None:
    manage_offices.close_office(office_id="ovest", actor_id=OWNER, **owner_stores(stores))
    assert [o.id for o in stores["offices"].active_offices()] == []
    assert [o.id for o in stores["offices"].all_offices()] == ["ovest"]


def test_a_closed_office_answers_nobody(stores) -> None:
    """Not even its own group chat. Being in an office's chat is the credential, and a
    closed office has none to offer."""
    offices = stores["offices"]
    offices.link_telegram_user("anya", 777)
    manage_offices.close_office(office_id="ovest", actor_id=OWNER, **owner_stores(stores))

    in_chat = resolve(
        chat_id=-100123,
        user_id=777,
        is_private=False,
        offices=offices,
        admin_ids=frozenset(),
        today=TODAY,
    )
    assert in_chat.audience is Audience.STRANGER

    privately = resolve(
        chat_id=777,
        user_id=777,
        is_private=True,
        offices=offices,
        admin_ids=frozenset(),
        today=TODAY,
    )
    assert privately.audience is Audience.STRANGER


async def test_a_closed_office_does_not_announce_a_roster(sessions, stores) -> None:
    """Per-office jobs are registered at boot and outlive the office, so closing one has
    to be refused by the thing that sends rather than by the job not firing."""
    from tabelshchik.application.send_attendance_reminder import (
        ReminderSkip,
        send_attendance_reminder,
    )
    from tabelshchik.application.voice import MoodPolicy, Voice

    manage_offices.close_office(office_id="ovest", actor_id=OWNER, **owner_stores(stores))
    notifier = RecordingNotifier()
    outcome = await send_attendance_reminder(
        office_id="ovest",
        offices=stores["offices"],
        schedule=SqlScheduleStore(sessions),
        notifier=notifier,
        voice=Voice(catalog=None, moods=MoodPolicy(weights={})),  # type: ignore[arg-type]
        clock=FixedClock(NOW),
        silent_policy=None,  # type: ignore[arg-type]
    )
    assert outcome.skipped is ReminderSkip.INACTIVE
    assert not notifier.messages


def test_closing_twice_says_so(stores) -> None:
    manage_offices.close_office(office_id="ovest", actor_id=OWNER, **owner_stores(stores))
    with pytest.raises(OfficeError, match="уже закрыт"):
        manage_offices.close_office(office_id="ovest", actor_id=OWNER, **owner_stores(stores))


def test_only_the_owner_may_close_an_office(stores) -> None:
    with pytest.raises(OfficeError, match="владелец"):
        manage_offices.close_office(office_id="ovest", actor_id=ADMIN, **owner_stores(stores))


# ------------------------------------------------------------------------------ delete


def test_deleting_needs_the_name_typed_out(stores) -> None:
    with pytest.raises(OfficeError, match="не совпало"):
        manage_offices.delete_office(
            office_id="ovest", confirmation="ovest", actor_id=OWNER, **owner_stores(stores)
        )
    assert stores["offices"].get_office("ovest") is not None


def test_the_typed_name_ignores_case_and_padding(stores) -> None:
    manage_offices.delete_office(
        office_id="ovest", confirmation="  o'vest  ", actor_id=OWNER, **owner_stores(stores)
    )
    assert stores["offices"].get_office("ovest") is None


def test_deleting_takes_the_whole_history_with_it(stores) -> None:
    """The reason it is behind a typed confirmation rather than a Да/Отмена pair."""
    manage_offices.delete_office(
        office_id="ovest", confirmation="O'Vest", actor_id=OWNER, **owner_stores(stores)
    )
    assert stores["offices"].employees("ovest") == []
    assert stores["offices"].office_of("anya") is None


def test_only_the_owner_may_delete_an_office(stores) -> None:
    with pytest.raises(OfficeError, match="владелец"):
        manage_offices.delete_office(
            office_id="ovest", confirmation="O'Vest", actor_id=ADMIN, **owner_stores(stores)
        )
