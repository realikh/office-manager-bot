"""Who the bot will talk to.

Every case here was answerable before this gate existed. The one that matters most is the
first: an unknown Telegram account in a private chat used to be handed the alphabetically
first office and told, by name, who was in tomorrow.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from tabelshchik.adapters.db.repositories import SqlOfficeStore, SqlRosterStore
from tabelshchik.application.audience import Audience, Claim, claim, linked, resolve

from .conftest import office_seed, seed

TODAY = date(2026, 9, 11)
OVEST_CHAT = -100123
PINE_CHAT = -100999
ANYA = 777
ADMIN = 555
STRANGER = 31337


@pytest.fixture
def offices(sessions):
    seed(
        sessions,
        office_seed(),
        office_seed(
            id="pine",
            name="Pine",
            chatId=PINE_CHAT,
            employees=[{"id": "petya", "name": "Петя"}],
            schedule={"vacantDesks": {"monday": 1}},
        ),
    )
    SqlOfficeStore(sessions).link_telegram_user("anya", ANYA)
    return sessions


def grant(sessions, *, chat_id=0, user_id=None, is_private=True, admins=frozenset()):
    return resolve(
        chat_id=chat_id,
        user_id=user_id,
        is_private=is_private,
        offices=SqlOfficeStore(sessions),
        admin_ids=admins,
        today=TODAY,
    )


# ------------------------------------------------------------------------- strangers


def test_an_unknown_account_in_private_gets_nothing(offices) -> None:
    """The hole this closes. It used to fall through to `active_offices()[0]`, so anyone
    who found the bot could ask who was in the office tomorrow and be told."""
    result = grant(offices, user_id=STRANGER)
    assert result.audience is Audience.STRANGER
    assert result.office_id is None
    assert not result.may_answer


def test_a_group_that_is_not_an_office_chat_gets_nothing(offices) -> None:
    result = grant(offices, chat_id=-100777, user_id=ANYA, is_private=False)
    assert result.audience is Audience.STRANGER
    assert result.office_id is None


def test_even_a_linked_employee_gets_nothing_in_an_unrelated_group(offices) -> None:
    """Being an employee does not make some random group an office chat. Answering there
    would publish one office's roster into a room nobody configured."""
    assert grant(offices, chat_id=-1, user_id=ANYA, is_private=False).office_id is None


def test_a_message_with_no_sender_gets_nothing(offices) -> None:
    assert grant(offices, user_id=None).audience is Audience.STRANGER


# --------------------------------------------------------------------- office chats


def test_an_office_chat_is_its_own_credential(offices) -> None:
    """Everyone in it already receives the full tagged roster every working day."""
    result = grant(offices, chat_id=OVEST_CHAT, user_id=STRANGER, is_private=False)
    assert result.audience is Audience.OFFICE_CHAT
    assert result.office_id == "ovest"


def test_each_office_chat_answers_for_its_own_office(offices) -> None:
    assert grant(offices, chat_id=PINE_CHAT, is_private=False).office_id == "pine"


def test_an_office_chat_still_recognises_a_linked_asker(offices) -> None:
    """So personal grounding — "your next days" — keeps working in the group."""
    result = grant(offices, chat_id=OVEST_CHAT, user_id=ANYA, is_private=False)
    assert result.employee is not None and result.employee.id == "anya"


# ------------------------------------------------------------------------ employees


def test_a_linked_employee_gets_their_own_office(offices) -> None:
    result = grant(offices, user_id=ANYA)
    assert result.audience is Audience.EMPLOYEE
    assert result.office_id == "ovest"
    assert result.employee is not None and result.employee.id == "anya"


def test_someone_who_has_left_gets_nothing(offices) -> None:
    """Ending a tenure leaves the Telegram link in place on purpose, so a restore just
    works. Without a tenure check that meant a leaver kept the whole office's week."""
    SqlRosterStore(offices).remove_employee("anya", ended_on=TODAY - timedelta(days=1))
    assert grant(offices, user_id=ANYA).audience is Audience.STRANGER


def test_somebody_who_has_not_started_yet_gets_nothing(offices) -> None:
    SqlRosterStore(offices).add_employee(
        "ovest", "future", "Будущий", started_on=TODAY + timedelta(days=30)
    )
    SqlOfficeStore(offices).link_telegram_user("future", 4242)
    assert grant(offices, user_id=4242).audience is Audience.STRANGER


def test_the_last_day_still_counts(offices) -> None:
    SqlRosterStore(offices).remove_employee("anya", ended_on=TODAY)
    assert grant(offices, user_id=ANYA).audience is Audience.EMPLOYEE


# --------------------------------------------------------------------------- admins


def test_an_admin_who_is_not_on_any_roster_still_gets_an_office(offices) -> None:
    result = grant(offices, user_id=ADMIN, admins=frozenset({ADMIN}))
    assert result.audience is Audience.ADMIN
    assert result.office_id == "ovest"


def test_an_admin_who_is_an_employee_gets_their_own_office(offices) -> None:
    SqlOfficeStore(offices).link_telegram_user("petya", ADMIN)
    result = grant(offices, user_id=ADMIN, admins=frozenset({ADMIN}))
    assert result.audience is Audience.ADMIN
    assert result.office_id == "pine"


def test_admin_rights_come_from_the_environment_not_the_roster(offices) -> None:
    """Impersonating an employee must never grant admin, whatever else it grants."""
    assert grant(offices, user_id=ANYA).audience is Audience.EMPLOYEE


# ------------------------------------------------------------------- claiming a record


def claim_as(sessions, *, user_id, username, today=TODAY):
    return claim(SqlOfficeStore(sessions), user_id=user_id, username=username, today=today)


def test_an_unmatched_username_is_unknown(offices) -> None:
    assert claim_as(offices, user_id=STRANGER, username="nobody").outcome is Claim.UNKNOWN
    assert claim_as(offices, user_id=STRANGER, username=None).outcome is Claim.UNKNOWN


def test_an_unlinked_record_may_be_claimed(offices) -> None:
    """Боря has a handle recorded but has never run /start."""
    SqlRosterStore(offices).set_username("borya", "borya_tg")
    result = claim_as(offices, user_id=STRANGER, username="borya_tg")
    assert result.outcome is Claim.GRANTED
    assert result.employee is not None and result.employee.id == "borya"


def test_a_leading_at_sign_is_tolerated(offices) -> None:
    SqlRosterStore(offices).set_username("borya", "borya_tg")
    assert claim_as(offices, user_id=STRANGER, username="@borya_tg").outcome is Claim.GRANTED


def test_a_record_somebody_else_holds_is_refused(offices) -> None:
    """The case that does damage. `link_telegram_user` overwrites, so a successful claim
    here would silently unlink the real person and redirect every reminder mention."""
    result = claim_as(offices, user_id=STRANGER, username="anya_tg")
    assert result.outcome is Claim.TAKEN
    assert result.employee is not None and result.employee.id == "anya"


def test_reclaiming_your_own_record_is_not_a_conflict(offices) -> None:
    assert claim_as(offices, user_id=ANYA, username="anya_tg").outcome is Claim.LINKED


def test_a_departed_person_cannot_be_claimed(offices) -> None:
    SqlRosterStore(offices).remove_employee("borya", ended_on=TODAY - timedelta(days=1))
    SqlRosterStore(offices).set_username("borya", "borya_tg")
    assert claim_as(offices, user_id=STRANGER, username="borya_tg").outcome is Claim.UNKNOWN


def test_a_departed_person_is_indistinguishable_from_an_unknown_one(offices) -> None:
    """Otherwise /start becomes an oracle for testing usernames against the roster."""
    SqlRosterStore(offices).remove_employee("borya", ended_on=TODAY - timedelta(days=1))
    SqlRosterStore(offices).set_username("borya", "borya_tg")
    departed = claim_as(offices, user_id=STRANGER, username="borya_tg")
    invented = claim_as(offices, user_id=STRANGER, username="nobody_at_all")
    assert departed.outcome is invented.outcome is Claim.UNKNOWN


def test_an_already_linked_employee_needs_no_write(offices) -> None:
    result = claim_as(offices, user_id=ANYA, username="anya_tg")
    assert result.outcome is Claim.LINKED
    assert result.employee is not None and result.employee.id == "anya"


# ------------------------------------------------------- the gate runs before anything


CHAT_SOURCE = Path("src/tabelshchik/adapters/telegram/routers/chat.py").read_text("utf-8")


def test_the_gate_runs_before_the_bot_reads_caches_or_answers() -> None:
    """Ordering is the whole protection.

    Caching a stranger's messages, reading an office's memory, or reaching the model are
    all things that must not happen for someone with no entitlement — and each of them
    used to happen, because the office was resolved with a fallback that never failed.
    """
    talk = CHAT_SOURCE[CHAT_SOURCE.index("async def talk") :]
    gate = talk.index("grant = resolve(")
    for later in ("_remember(services, message", "reply = await answer("):
        assert gate < talk.index(later), f"{later.strip()} happens before the gate"


def test_nothing_falls_back_to_the_first_office_any_more() -> None:
    """The exact line that made every stranger an insider."""
    assert "active[0].id" not in CHAT_SOURCE
    assert "_office_for" not in CHAT_SOURCE


# --------------------------------------------------------------------------- linked()


def test_linked_ignores_an_unknown_id(offices) -> None:
    assert linked(SqlOfficeStore(offices), STRANGER, TODAY) is None
    assert linked(SqlOfficeStore(offices), None, TODAY) is None
