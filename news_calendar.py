"""
News Blackout Calendar
=======================
Static, offline calendar of high-impact recurring economic events.

The bot calls :func:`in_blackout(symbol, now_et)` before dispatching
strategy signals. A ``True`` response means a known event window
overlaps ``now_et`` and the caller should suppress new entries for
the listed symbols (or all enabled symbols if the event's
``symbols`` list is empty).

Only the cadences actually used in ``news_events.json`` are
supported. Extending the calendar requires *both* adding an entry to
``news_events.json`` *and* making sure its ``cadence`` value is
handled in :func:`_event_fires_today`.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Optional

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_CALENDAR_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "news_events.json"
)

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _load_events(path: str = _CALENDAR_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("news_calendar: failed to load %s: %s", path, e)
        return []
    return data.get("events", [])


def _parse_time(t_str: str) -> time:
    h, m = t_str.split(":")
    return time(int(h), int(m))


def _nth_weekday(year: int, month: int, week: int, weekday: int) -> Optional[date]:
    """Return the date of the n-th weekday in a month (e.g. 1st Friday).

    week=1..5; weekday 0=Monday..6=Sunday.
    """
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = 1 + offset + (week - 1) * 7
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_weekday_list(raw: Optional[str]) -> set[int]:
    if not raw:
        return set(range(7))
    return {_WEEKDAYS[w.strip().lower()] for w in raw.split(",") if w.strip()}


def _event_fires_today(event: dict, today: date) -> bool:
    """True if ``event`` is scheduled on ``today`` (ET date)."""
    cadence = event.get("cadence", "")
    allowed_weekdays = _parse_weekday_list(event.get("weekday"))

    if cadence == "daily":
        return today.weekday() < 5  # skip weekends

    if cadence == "weekly":
        return today.weekday() in allowed_weekdays

    if cadence == "monthly_nth_weekday":
        week = int(event.get("week", 1))
        wd_name = event.get("weekday", "friday")
        wd = _WEEKDAYS[wd_name.lower()]
        scheduled = _nth_weekday(today.year, today.month, week, wd)
        return scheduled == today

    if cadence == "monthly_day_approx":
        target_day = int(event.get("day", 15))
        # Events released "around" a day typically slip ±3 calendar days.
        if abs(today.day - target_day) > 3:
            return False
        return today.weekday() in allowed_weekdays

    if cadence == "quarterly_approx":
        month_days = event.get("month_days", {})
        target_day = month_days.get(str(today.month))
        if target_day is None:
            return False
        if abs(today.day - int(target_day)) > 3:
            return False
        return today.weekday() < 5

    if cadence == "fomc_schedule":
        # Conservative fallback: treat a fixed list of historical FOMC
        # dates as authoritative, otherwise assume any given day is
        # *not* FOMC. Updating this list is cheaper than calling a web
        # calendar and makes the function deterministic for tests.
        fomc_dates = _fomc_meeting_dates()
        return today in fomc_dates

    return False


# Known FOMC meeting dates for 2025–2026. Update before the start of
# each year. Source: federalreserve.gov/monetarypolicy/fomccalendars.htm
_FOMC_DATES_RAW = [
    "2025-01-29", "2025-03-19", "2025-04-30", "2025-06-18", "2025-07-30",
    "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
    "2026-09-16", "2026-10-28", "2026-12-09",
]


def _fomc_meeting_dates() -> set[date]:
    return {date.fromisoformat(d) for d in _FOMC_DATES_RAW}


def _window_for(event: dict, today: date) -> Optional[tuple[datetime, datetime]]:
    """Return the (start, end) datetime window for an event on ``today``
    in ET, or None if the event doesn't fire today.
    """
    if not _event_fires_today(event, today):
        return None
    try:
        t = _parse_time(event["time_et"])
    except (KeyError, ValueError):
        return None
    window = int(event.get("window_minutes", 15))
    center = datetime.combine(today, t, tzinfo=_ET)
    return center - timedelta(minutes=window), center + timedelta(minutes=window)


def in_blackout(symbol: str, now_et: Optional[datetime] = None,
                events: Optional[list[dict]] = None) -> tuple[bool, Optional[dict]]:
    """Check whether ``symbol`` should be blocked by a news event right now.

    Args:
        symbol: the base symbol (e.g. "MCL"). Events with an empty
            ``symbols`` list apply globally.
        now_et: current time in ET — defaults to ``datetime.now(_ET)``.
        events: optional pre-loaded calendar (for tests).

    Returns ``(blocked, event_dict_or_None)``.
    """
    if now_et is None:
        now_et = datetime.now(_ET)
    if events is None:
        events = _load_events()

    today = now_et.date()
    for event in events:
        symbols = event.get("symbols") or []
        if symbols and symbol not in symbols:
            continue
        window = _window_for(event, today)
        if window is None:
            continue
        start, end = window
        if start <= now_et <= end:
            return True, event
    return False, None
