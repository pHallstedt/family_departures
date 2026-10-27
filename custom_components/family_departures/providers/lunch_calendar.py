"""Lunch menu from a Home Assistant calendar entity (plan §7.2).

The HACS ``skolmat`` integration (and similar) exposes one calendar entity per
school with one event per day, all-day by default, whose summary is the menu.
This adapter reads that day through ``calendar.get_events``. It accepts all-day
events, which the schedule provider drops, and it adds no outbound host: the
calendar integration does the fetching.

Status follows plan §7.2: a missing or erroring entity is ``error`` (never an
empty menu), a successful fetch with no event is ``empty`` (weekend, holiday).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from ..models import LunchMenu
from ..timeutil import local_day_bounds
from .ha_calendar import async_get_calendar_events

MAX_DISHES = 5
MAX_DISH_LENGTH = 120

_DISH_SEPARATOR = re.compile(r"\n| \| ")


def split_dishes(summary: str) -> list[str]:
    """Split an event summary into trimmed dishes, on newlines and `` | ``."""
    return [part.strip() for part in _DISH_SEPARATOR.split(summary) if part.strip()]


def _covers_day(item: dict[str, Any], d: date) -> bool:
    """Whether an all-day event spans ``d``; timed events already passed the window.

    ``get_events`` matches by instant in Home Assistant's time zone, so an
    all-day event for the neighbouring day can leak into the window when the
    zones differ; the dates are authoritative.
    """
    start, end = item.get("start"), item.get("end")
    if not (isinstance(start, str) and "T" not in start):
        return True
    try:
        first = date.fromisoformat(start)
        last = (
            date.fromisoformat(end) if isinstance(end, str) and "T" not in end else None
        )
    except ValueError:
        return False
    return first <= d and (last is None or d < last)


class HaCalendarLunchProvider:
    """Reads one local day's lunch menu from a HA calendar entity."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self._hass = hass
        self._entity_id = entity_id

    async def async_get_lunch(
        self, d: date, *, now: datetime | None = None
    ) -> LunchMenu:
        fetched_at = now or datetime.now(UTC)
        start_utc, end_utc = local_day_bounds(d)
        raw, _error_code = await async_get_calendar_events(
            self._hass, self._entity_id, start_utc, end_utc, "lunch"
        )
        if raw is None:
            return LunchMenu(
                local_date=d, status="error", dishes=(), fetched_at=fetched_at
            )
        dishes: list[str] = []
        for item in raw:
            if not _covers_day(item, d):
                continue
            dishes.extend(split_dishes(str(item.get("summary") or "")))
        capped = tuple(dish[:MAX_DISH_LENGTH] for dish in dishes[:MAX_DISHES])
        return LunchMenu(
            local_date=d,
            status="ok" if capped else "empty",
            dishes=capped,
            fetched_at=fetched_at,
        )
