"""Public holidays, from the `holidays` package.

The domain deliberately does not compute holidays — it takes a set of concrete dates —
so this is the only place that knows a country code exists.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import holidays


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
