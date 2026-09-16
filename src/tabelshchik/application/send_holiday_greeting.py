"""Congratulating the office on a public holiday.

Checked every day, not worked out in advance: the job asks the holiday calendar about
*today* each time it runs, so a calendar that changed since the bot started — a new
package release, a decree — is what it answers from.

Only a holiday that is about something is greeted. A weekend holiday moved onto Monday,
or a bridge day transferred by decree, is a day off without an occasion, and the second
and third days of Nauryz are the same holiday as the first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from tabelshchik.application.policy import SilentPolicy
from tabelshchik.application.ports import (
    Celebration,
    Clock,
    HolidayCalendar,
    Notifier,
    OfficeStore,
    PostKind,
    PostRecord,
    PostStore,
)
from tabelshchik.application.voice import CommonText, Voice

#: Holidays where irony reads as disrespect, whatever the bot's mood that day. Matched on
#: the English name the calendar provides, which is the same for every language of the
#: country's own names.
SOLEMN_WORDS = (
    "christmas",
    "easter",
    "eid",
    "adha",
    "fitr",
    "ramadan",
    "kurban",
    "victory",
    "defender",
    "memorial",
    "remembrance",
    "mourning",
)


@dataclass(frozen=True, slots=True)
class GreetingOutcome:
    office_id: str
    holidays: tuple[str, ...] = ()
    text: str = ""
    sent: bool = False
    silent: bool = False
    generated: bool = False
    skipped: str | None = None


async def send_holiday_greeting(
    *,
    office_id: str,
    offices: OfficeStore,
    notifier: Notifier,
    voice: Voice,
    clock: Clock,
    calendar: HolidayCalendar,
    posts: PostStore,
    silent_policy: SilentPolicy,
    chat_id: int | None = None,
    dry_run: bool = False,
) -> GreetingOutcome:
    office = offices.get_office(office_id)
    if office is None or not office.active:
        # Per-office jobs outlive the office; a closed one congratulates nobody.
        return GreetingOutcome(office_id, skipped="inactive")

    today = clock.today()
    occasions = new_celebrations(calendar, office.holiday_calendar, today)
    if not occasions:
        return GreetingOutcome(office_id, skipped="not-a-holiday")
    names = tuple(item.name for item in occasions)

    destination = chat_id if chat_id is not None else office.chat_id
    recording = not dry_run and chat_id is None
    if recording:
        if posts.sent(office_id, PostKind.HOLIDAY, today):
            return GreetingOutcome(office_id, names, skipped="already-sent")
        # Two offices in one group would otherwise wish it a happy holiday twice.
        if destination is not None and posts.sent_to_chat(destination, PostKind.HOLIDAY, today):
            return GreetingOutcome(office_id, names, skipped="chat-already-greeted")

    mood = voice.mood_for(office_id=office_id, day=today)
    common = voice.catalog.common
    fallback = voice.static(
        "holiday.greeting",
        mood,
        office_id=office_id,
        day=today,
        holiday=", ".join(f"«{item.local_name}»" for item in occasions),
    )
    announcement = await voice.announce(
        mood,
        brief=holiday_brief(today, occasions, common),
        fallback=fallback,
        office_name=office.name,
        forbidden=[employee.full_name for employee in offices.employees(office_id)],
        allow_words=(common.weekdays[today.weekday()], common.months[today.month - 1]),
        allow_numbers=(today.day,),
    )
    text = announcement.text
    generated = announcement.generated
    silent = silent_policy.is_silent(today.weekday(), clock.time_of_day())

    if dry_run:
        return GreetingOutcome(office_id, names, text=text, silent=silent, generated=generated)
    if destination is None:
        return GreetingOutcome(office_id, names, text=text, skipped="no-chat")

    sent = await notifier.send(destination, text, silent=silent)
    if sent is None:
        return GreetingOutcome(office_id, names, text=text, skipped="send-failed")

    if recording:
        posts.record(
            PostRecord(
                office_id=office_id,
                kind=PostKind.HOLIDAY,
                day=today,
                chat_id=destination,
                message_id=sent.message_id,
            ),
            at=clock.now(),
        )
    return GreetingOutcome(
        office_id, names, text=text, sent=True, silent=silent, generated=generated
    )


def new_celebrations(calendar: HolidayCalendar, country: str, today: date) -> list[Celebration]:
    """Today's holidays that did not already start yesterday."""
    yesterday = {item.name for item in calendar.celebrations(country, today - timedelta(days=1))}
    return [item for item in calendar.celebrations(country, today) if item.name not in yesterday]


def is_solemn(occasions: list[Celebration]) -> bool:
    return any(word in item.name.casefold() for item in occasions for word in SOLEMN_WORDS)


def holiday_brief(today: date, occasions: list[Celebration], common: CommonText) -> str:
    weekday = common.weekdays[today.weekday()]
    month = common.months[today.month - 1]
    named = "; ".join(f"{item.name} ({item.local_name})" for item in occasions)
    lines = [
        f"Сегодня {weekday}, {today.day} {month}, праздник: {named}.",
        "Поздравь коллег в рабочем чате с этим праздником — тепло и по-человечески, "
        "в своём сегодняшнем настроении. Название праздника напиши по-русски.",
    ]
    if is_solemn(occasions):
        lines.append(
            "Это религиозный или памятный праздник: никакой иронии и сарказма, только "
            "искренне и уважительно — даже если настроение у тебя сегодня ворчливое или "
            "мрачное."
        )
    else:
        lines.append(
            "Можно пошутить, но не над самим праздником и не над людьми — только над "
            "обстоятельствами."
        )
    lines.append(
        f"Можно назвать сегодняшнее число ({today.day}) и месяц; другие даты, числа и дни "
        "недели не упоминай. О работе, расписании и офисе не пиши."
    )
    return "\n".join(lines)
