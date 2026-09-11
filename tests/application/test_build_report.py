from __future__ import annotations

from datetime import date, timedelta

from tabelshchik.adapters.reports.xlsx import PALETTE, build_workbook, upcoming_week
from tabelshchik.application.build_report import (
    ReportDay,
    build_report,
    compute_statistics,
    surplus_days,
)
from tabelshchik.domain.calendar import CalendarSpec
from tabelshchik.domain.entities import SCALE, Employee, LedgerEntry

from .test_voice import COMMON

START = date(2026, 9, 14)
END = START + timedelta(days=13)


def people(count: int = 3) -> list[Employee]:
    return [Employee(id=f"e{i}", full_name=f"Сотрудник {i}") for i in range(count)]


def days(*attendance: tuple[int, tuple[str, ...]]) -> list[ReportDay]:
    return [
        ReportDay(
            day=START + timedelta(days=offset),
            weekday=(START + timedelta(days=offset)).weekday(),
            attendees=who,
            desks_required=len(who),
        )
        for offset, who in attendance
    ]


def report(**kwargs):
    defaults = {
        "office_name": "O'Vest",
        "start": START,
        "end": END,
        "employees": people(),
        "days": days((0, ("e0", "e1")), (1, ("e1", "e2"))),
        "spec": CalendarSpec(),
    }
    return build_report(**{**defaults, **kwargs})


# ------------------------------------------------------------------------ statistics


def test_totals_count_attendance() -> None:
    stats = report().stats
    assert stats.totals == {"e0": 1, "e1": 2, "e2": 1}


def test_the_spread_is_max_minus_min() -> None:
    assert report().stats.spread == 1


def test_the_spread_of_an_empty_period_is_zero() -> None:
    assert report(days=[]).stats.spread == 0


def test_weekday_counts_are_tracked_for_the_fairness_view() -> None:
    stats = report().stats
    assert stats.per_weekday["e1"][0] == 1  # Monday
    assert stats.per_weekday["e1"][1] == 1  # Tuesday


def test_share_sums_to_one_across_the_roster() -> None:
    stats = report().stats
    assert round(sum(stats.share(f"e{i}") for i in range(3)), 6) == 1.0


def test_first_and_last_day_are_recorded() -> None:
    stats = report().stats
    assert stats.first_day["e1"] == START
    assert stats.last_day["e1"] == START + timedelta(days=1)


def test_someone_who_never_came_has_no_first_day() -> None:
    stats = report(days=days((0, ("e0",)))).stats
    assert "e2" not in stats.first_day
    assert stats.totals["e2"] == 0


def test_the_mean_gap_needs_at_least_two_visits() -> None:
    stats = report(days=days((0, ("e0", "e1")), (7, ("e1",)))).stats
    assert stats.mean_gap["e1"] == 7.0
    assert "e0" not in stats.mean_gap


def test_shortfall_is_desks_offered_minus_filled() -> None:
    stats = compute_statistics(
        report().employees,
        [ReportDay(day=START, weekday=0, attendees=("e0",), desks_required=3)],
    )
    assert stats.shortfall == 2


# --------------------------------------------------------------------------- roster


def test_someone_who_left_before_the_period_is_excluded() -> None:
    """Otherwise they sit at zero days and make the spread look terrible for no reason."""
    roster = [*people(2), Employee(id="gone", full_name="Ушёл", ended_on=START - timedelta(days=1))]
    result = report(employees=roster, days=days((0, ("e0", "e1"))))

    assert [employee.id for employee in result.employees] == ["e0", "e1"]
    assert result.stats.spread == 0


def test_someone_joining_after_the_period_is_excluded() -> None:
    roster = [
        *people(2),
        Employee(id="later", full_name="Позже", started_on=END + timedelta(days=1)),
    ]
    assert "later" not in {employee.id for employee in report(employees=roster).employees}


def test_someone_joining_mid_period_is_included() -> None:
    roster = [
        *people(2),
        Employee(id="mid", full_name="Середина", started_on=START + timedelta(days=3)),
    ]
    assert "mid" in {employee.id for employee in report(employees=roster).employees}


def test_colours_are_stable_for_a_given_roster() -> None:
    first = {e.id: e.colour_index for e in report().employees}
    second = {e.id: e.colour_index for e in report().employees}
    assert first == second


def test_the_roster_is_ordered_by_name() -> None:
    roster = [
        Employee(id="b", full_name="Борис"),
        Employee(id="a", full_name="Анна"),
    ]
    assert [e.name for e in report(employees=roster, days=[]).employees] == ["Анна", "Борис"]


# --------------------------------------------------------------------------- surplus


def test_surplus_comes_through_from_the_ledger() -> None:
    ledger = {"e0": LedgerEntry("e0", days=5, entitlement_scaled=3 * SCALE)}
    result = report(ledger=ledger)
    assert surplus_days(result.employee("e0")) == 2.0
    assert surplus_days(result.employee("e1")) == 0.0


# -------------------------------------------------------------------- skipped days


def test_weekday_closures_are_reported_with_a_reason() -> None:
    spec = CalendarSpec(closed=frozenset({START}), holiday_names={START: "Переезд"})
    assert report(spec=spec).skipped[0] == (START, "Переезд")


def test_weekends_are_not_reported_as_skipped() -> None:
    """Nobody needs a sheet telling them Saturday was a Saturday."""
    assert report().skipped == ()


# ------------------------------------------------------------------------- workbook


def test_the_workbook_is_a_readable_xlsx() -> None:
    import io
    import zipfile

    content = build_workbook(report(), COMMON)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert "xl/workbook.xml" in archive.namelist()
        sheets = archive.read("xl/workbook.xml").decode()

    for name in ("Расписание", "Компактный вид", "Статистика"):
        assert name in sheets


def test_the_skipped_sheet_appears_only_when_something_was_skipped() -> None:
    import io
    import zipfile

    spec = CalendarSpec(closed=frozenset({START}))
    with zipfile.ZipFile(io.BytesIO(build_workbook(report(spec=spec), COMMON))) as archive:
        assert "Пропущенные дни" in archive.read("xl/workbook.xml").decode()

    with zipfile.ZipFile(io.BytesIO(build_workbook(report(), COMMON))) as archive:
        assert "Пропущенные дни" not in archive.read("xl/workbook.xml").decode()


def test_a_roster_larger_than_the_palette_still_gets_colours() -> None:
    roster = [Employee(id=f"p{i}", full_name=f"Сотрудник {i:02d}") for i in range(len(PALETTE) + 5)]
    content = build_workbook(report(employees=roster, days=[]), COMMON)
    assert len(content) > 0


def test_an_empty_period_still_produces_a_workbook() -> None:
    assert len(build_workbook(report(days=[], employees=[]), COMMON)) > 0


def test_the_upcoming_week_preview_skips_empty_days() -> None:
    preview = upcoming_week(report(days=days((0, ("e0",)), (1, ()))), COMMON)
    assert preview.count("\n") == 0
    assert "пн 14.09" in preview
