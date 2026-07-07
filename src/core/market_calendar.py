"""US equity market trading-day calendar — stdlib only (no external deps, per project constraint).

Weekends + NYSE holidays. Holidays are COMPUTED for any year (fixed-date with weekend
observance, nth-weekday rules, and Good Friday via the Easter algorithm) so there is no
hardcoded list to go stale.

Scope/limitations (honest):
- Day-level only. Does NOT model half-days (early closes around Thanksgiving/July 4/Christmas);
  those are still trading days here. Fine for a paper runner that gates LLM spend by day.
- Juneteenth is included (NYSE holiday since 2022); correct for 2026+ usage. For years < 2022
  this would over-mark one day/year as closed — irrelevant to this project's forward runs.
- Rule-of-thumb ad-hoc closures (e.g. national days of mourning, weather) are not modeled.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Optional, Union


def _easter(year: int) -> date:
    """Anonymous Gregorian (Meeus/Jones/Butcher) algorithm — Easter Sunday for the year."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """NYSE observance: Saturday holiday -> observed prior Friday; Sunday -> observed next Monday."""
    if d.weekday() == 5:   # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:   # Sunday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth (1-based) occurrence of `weekday` (Mon=0) in month."""
    d = date(year, month, 1)
    count = 0
    while True:
        if d.weekday() == weekday:
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last occurrence of `weekday` (Mon=0) in month."""
    if month == 12:
        d = date(year, 12, 31)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def nyse_holidays(year: int) -> set:
    """Set of NYSE full-closure holidays for the given year (observed dates)."""
    return {
        _observed(date(year, 1, 1)),            # New Year's Day
        _nth_weekday(year, 1, 0, 3),            # MLK Jr. Day — 3rd Monday Jan
        _nth_weekday(year, 2, 0, 3),            # Washington's Birthday — 3rd Monday Feb
        _easter(year) - timedelta(days=2),      # Good Friday
        _last_weekday(year, 5, 0),              # Memorial Day — last Monday May
        _observed(date(year, 6, 19)),           # Juneteenth
        _observed(date(year, 7, 4)),            # Independence Day
        _nth_weekday(year, 9, 0, 1),            # Labor Day — 1st Monday Sep
        _nth_weekday(year, 11, 3, 4),           # Thanksgiving — 4th Thursday Nov
        _observed(date(year, 12, 25)),          # Christmas Day
    }


def is_us_market_day(when: Optional[Union[date, datetime]] = None) -> bool:
    """True if `when` (default: today) is a regular US equity trading day (not weekend, not holiday)."""
    if when is None:
        d = date.today()
    elif isinstance(when, datetime):
        d = when.date()
    else:
        d = when
    if d.weekday() >= 5:  # Saturday/Sunday
        return False
    return d not in nyse_holidays(d.year)
