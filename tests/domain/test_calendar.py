from datetime import date, timedelta

from tabelshchik.domain.calendar import (
    CalendarSpec,
    DayKind,
    last_working_day_of_month,
    next_working_day,
    roll_forward_to_working_day,
    start_of_week,
    week_index,
    working_days,
)

MON = date(2026, 9, 14)
FRI = date(2026, 9, 18)
SAT = date(2026, 9, 19)
SUN = date(2026, 9, 20)


def test_weekend_is_not_a_working_day() -> None:
    spec = CalendarSpec()
    assert spec.is_working_day(MON)
    assert not spec.is_working_day(SAT)
    assert spec.kind(SUN) is DayKind.WEEKEND


def test_friday_reminder_targets_monday() -> None:
    # The whole point of "next working day": Fri->Mon needs no special case.
    assert next_working_day(CalendarSpec(), FRI) == MON + timedelta(days=7)


def test_sunday_reminder_targets_monday() -> None:
    assert next_working_day(CalendarSpec(), SUN) == date(2026, 9, 21)


def test_holiday_chain_is_skipped() -> None:
    spec = CalendarSpec(holidays=frozenset({date(2026, 9, 21), date(2026, 9, 22)}))
    assert next_working_day(spec, SUN) == date(2026, 9, 23)


def test_extra_workday_overrides_weekend_and_holiday() -> None:
    # Kazakhstan routinely transfers a holiday onto an otherwise-free Saturday.
    spec = CalendarSpec(holidays=frozenset({SAT}), extra_workdays=frozenset({SAT}))
    assert spec.is_working_day(SAT)
    assert next_working_day(spec, FRI) == SAT


def test_closed_day_is_skipped_with_a_reason() -> None:
    spec = CalendarSpec(closed=frozenset({MON}), holiday_names={MON: "Переезд офиса"})
    assert not spec.is_working_day(MON)
    assert spec.reason(MON) == "Переезд офиса"


def test_working_days_is_inclusive_of_both_ends() -> None:
    assert working_days(CalendarSpec(), MON, FRI) == [
        date(2026, 9, d) for d in (14, 15, 16, 17, 18)
    ]


def test_start_of_week_and_week_index() -> None:
    assert start_of_week(date(2026, 9, 17)) == MON
    assert week_index(MON, date(2026, 9, 17)) == 0
    assert week_index(MON, date(2026, 9, 23)) == 1


def test_week_index_rolls_over_the_new_year() -> None:
    anchor = date(2026, 12, 28)
    assert week_index(anchor, date(2027, 1, 4)) == 1


def test_last_working_day_of_month_skips_a_weekend_tail() -> None:
    # 31 Oct 2026 is a Saturday, so the month-end Tempo nag belongs on Friday the 30th.
    assert last_working_day_of_month(CalendarSpec(), date(2026, 10, 5)) == date(2026, 10, 30)


def test_last_working_day_of_month_handles_december() -> None:
    assert last_working_day_of_month(CalendarSpec(), date(2026, 12, 1)) == date(2026, 12, 31)


def test_roll_forward_keeps_a_working_day_where_it_is() -> None:
    spec = CalendarSpec()
    assert roll_forward_to_working_day(spec, MON) == MON
    assert roll_forward_to_working_day(spec, SAT) == date(2026, 9, 21)
