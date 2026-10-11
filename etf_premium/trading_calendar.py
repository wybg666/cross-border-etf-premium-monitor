"""Mainland exchange holiday guard, explicitly limited to calendar year 2026."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Set


# Weekends remain closed, including public-holiday make-up workdays.
# The maintained holiday table only covers 2026. Callers must surface an
# unknown-calendar status when is_calendar_supported() returns False.
CN_MARKET_HOLIDAYS: Set[str] = {
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    "2026-10-06", "2026-10-07",
}


def _as_date(value: Optional[date | datetime | str] = None) -> date:
    if value is None:
        return datetime.now(timezone(timedelta(hours=8))).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def _env_dates(name: str) -> Set[str]:
    dates: Set[str] = set()
    for part in os.environ.get(name, "").replace(";", ",").split(","):
        text = part.strip()
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            continue
        dates.add(parsed.isoformat())
    return dates


def cn_market_holidays() -> Set[str]:
    """Extra closures may be specified as comma-separated ISO dates."""
    return set(CN_MARKET_HOLIDAYS) | _env_dates("CN_MARKET_EXTRA_HOLIDAYS")


def is_calendar_supported(value: Optional[date | datetime | str] = None) -> bool:
    return _as_date(value).year == 2026


def is_cn_trading_day(value: Optional[date | datetime | str] = None) -> bool:
    """Return False for unknown years; this does NOT confirm market closure."""
    day = _as_date(value)
    return is_calendar_supported(day) and day.weekday() < 5 and day.isoformat() not in cn_market_holidays()
