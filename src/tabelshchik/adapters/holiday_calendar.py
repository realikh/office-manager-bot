"""Public holidays, from the `holidays` package.

The domain deliberately does not compute holidays — it takes a set of concrete dates —
so this is the only place that knows a country code exists.

The package is the source rather than a web API because it tracks the law: it already
knows Kazakhstan's Constitution Day moves to 15 March from 2027, while the free API that
was checked still lists 30 August and misses Orthodox Christmas and Kurban Ait altogether.
Its data changes only when the package does, which is why a scheduled workflow keeps the
pin in `uv.lock` current.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from functools import lru_cache
from importlib import metadata

import holidays

from tabelshchik.application.ports import Celebration


@lru_cache(maxsize=32)
def _calendar(country: str, years: tuple[int, ...]) -> dict[date, str]:
    """Cached because the previous implementation rebuilt this table on every call, and
    it is called several times per reminder."""
    try:
        table = holidays.country_holidays(country, years=list(years))
    except NotImplementedError:
        return {}
    return {day: str(name) for day, name in table.items()}


def public_holidays(country: str, start: date, end: date) -> dict[date, str]:
    """Holiday dates and their names within the inclusive range."""
    years = tuple(range(start.year, end.year + 1))
    table = _calendar(country.upper(), years)
    return {day: name for day, name in table.items() if start <= day <= end}


@lru_cache(maxsize=32)
def _celebrations(country: str, year: int) -> tuple[Celebration, ...]:
    """The year's holidays that are about something.

    `observed=False` drops the weekday a weekend holiday was moved onto. Transferred days
    off ("Day off (substituted from …)") are not covered by that flag, so they are
    recognised by the package's own label for them.
    """
    try:
        english = holidays.country_holidays(country, years=[year], observed=False, language="en_US")
        local = holidays.country_holidays(country, years=[year], observed=False)
    except NotImplementedError:
        return ()

    substituted = _label_prefix(english, "substituted_label")
    estimated = _label_suffix(english, "estimated_label")
    local_estimated = _label_suffix(local, "estimated_label")

    found: list[Celebration] = []
    for day in sorted(english):
        names = english.get_list(day)
        local_names = local.get_list(day)
        for index, name in enumerate(names):
            if substituted and name.startswith(substituted):
                continue
            local_name = local_names[index] if index < len(local_names) else name
            found.append(
                Celebration(
                    day=day,
                    name=_strip(name, estimated),
                    local_name=_strip(local_name, local_estimated),
                )
            )
    return tuple(found)


def _label_prefix(table: holidays.HolidayBase, attribute: str) -> str:
    """The part of a label before its `%s`, as the table's language renders it."""
    label = _translated(table, attribute)
    return label.split("%s", 1)[0] if "%s" in label else ""


def _label_suffix(table: holidays.HolidayBase, attribute: str) -> str:
    label = _translated(table, attribute)
    return label.split("%s", 1)[1] if "%s" in label else ""


def _translated(table: holidays.HolidayBase, attribute: str) -> str:
    raw = getattr(table, attribute, "")
    if not isinstance(raw, str) or not raw:
        return ""
    translate = getattr(table, "tr", None)
    return str(translate(raw)) if callable(translate) else raw


def _strip(name: str, suffix: str) -> str:
    return name[: -len(suffix)] if suffix and name.endswith(suffix) else name


class PackageHolidayCalendar:
    """`HolidayCalendar`, answered from the installed `holidays` package."""

    def celebrations(self, country: str, day: date) -> Sequence[Celebration]:
        return [item for item in _celebrations(country.upper(), day.year) if item.day == day]

    def next_celebration(self, country: str, after: date) -> Celebration | None:
        for year in (after.year, after.year + 1):
            for item in _celebrations(country.upper(), year):
                if item.day >= after:
                    return item
        return None

    def source(self) -> str:
        try:
            return f"holidays {metadata.version('holidays')}"
        except metadata.PackageNotFoundError:
            return "holidays"
