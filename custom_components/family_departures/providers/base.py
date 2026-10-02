"""Provider protocols for Family Departures (plan §3, spec §8).

These ``Protocol`` definitions are the async contracts the coordinator depends
on. Concrete adapters (ICS, HA calendar, SL journey, Waze) are implemented by
later tasks. This module stays free of a hard ``homeassistant`` dependency; the
concrete adapters may import it, but the contract does not.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Protocol, runtime_checkable

from ..models import DurationResult, JourneyResult, ScheduleResult


@runtime_checkable
class ScheduleProvider(Protocol):
    """Fetches one local day's schedule events."""

    async def async_get_day(self, d: date) -> ScheduleResult:
        """Return the schedule for the given local date."""
        ...


@runtime_checkable
class JourneyProvider(Protocol):
    """Plans public-transport journeys to a destination by a deadline."""

    async def async_plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        deadline: datetime,
        earliest_departure: datetime | None,
    ) -> JourneyResult:
        """Return journey candidates arriving by ``deadline``."""
        ...


@runtime_checkable
class CarProvider(Protocol):
    """Returns a door-to-door car travel duration."""

    async def async_get_duration(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        realtime: bool,
        time_delta: timedelta | None,
    ) -> DurationResult:
        """Return the travel duration for the given origin/destination."""
        ...
