"""Time helpers for the Family Departures integration.

All internal datetimes are timezone-aware UTC; local dates and day boundaries
are computed in ``Europe/Stockholm`` (plan §1 rules 3-4, spec §7). This module
is pure: it must not import ``homeassistant``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Stockholm")


def local_date_of(dt: datetime) -> date:
    """Return the ``Europe/Stockholm`` calendar date of an aware datetime.

    Raises ``ValueError`` for naive datetimes so a missing tzinfo cannot be
    silently interpreted as local time.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"local_date_of requires an aware datetime, got {dt!r}")
    return dt.astimezone(TZ).date()


def local_day_bounds(d: date) -> tuple[datetime, datetime]:
    """Return the UTC start and end of a local day as aware datetimes.

    The start is local midnight; the end is local midnight of the next day.
    Both are returned in UTC. This correctly spans 23- and 25-hour DST days.
    """
    start_local = datetime.combine(d, time(0, 0), tzinfo=TZ)
    end_local = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=TZ)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def combine_local(d: date, t: time) -> datetime:
    """Combine a local date and time into an aware UTC datetime.

    The wall-clock time ``t`` is interpreted in ``Europe/Stockholm`` and
    converted to UTC.
    """
    local = datetime.combine(d, t.replace(tzinfo=None), tzinfo=TZ)
    return local.astimezone(UTC)


def make_mission_id(person_id: str, d: date, slot: str = "morning") -> str:
    """Build a stable mission key ``person_id:YYYY-MM-DD:slot`` (spec §8)."""
    return f"{person_id}:{d.isoformat()}:{slot}"
