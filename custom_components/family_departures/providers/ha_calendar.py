"""Home Assistant calendar schedule provider (spec §4.3, §5.3).

Reads a full local day from a Home Assistant calendar entity (e.g. a Local
Calendar) through the ``calendar.get_events`` action. The calendar entity's
``state``/attributes only expose the current/next event, so the whole-day
query goes through the service with ``return_response=True`` [S4].

The same provider also answers the household "holiday" question: an all-day
``Ledig`` event in a shared calendar marks a day off (spec §5.4).

Result semantics follow spec §5.3/§5.4: a missing or erroring entity is a
``status="error"`` (never an empty day), a successful fetch with no matching
events is ``status="empty"``, and anything else is ``status="ok"``.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from ..event_tags import parse_event_tags, strip_tags
from ..models import ScheduleEvent, ScheduleResult, SourceFilter
from ..timeutil import local_day_bounds

_LOGGER = logging.getLogger(__name__)

CALENDAR_DOMAIN = "calendar"
SERVICE_GET_EVENTS = "get_events"

# Error codes surfaced on ``ScheduleResult`` so the coordinator/dashboard can
# tell failure modes apart without leaking details.
ERROR_UNAVAILABLE = "entity_unavailable"
ERROR_SERVICE = "service_error"
ERROR_RESPONSE = "bad_response"

# Default summary that marks a household day off (spec §5.4).
HOLIDAY_SUMMARY = "Ledig"


def _matches_any(summary: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive substring match of ``summary`` against ``patterns``."""
    folded = summary.casefold()
    return any(pattern.casefold() in folded for pattern in patterns)


def _is_all_day(value: object) -> bool:
    """Return ``True`` when a ``get_events`` bound is a date-only (all-day) value.

    The service renders a timed event as a full ISO datetime (with a ``T``
    separator) and an all-day event as a date-only ``YYYY-MM-DD`` string.
    """
    return isinstance(value, str) and "T" not in value


def _parse_bound(value: object) -> datetime | None:
    """Parse a timed ``get_events`` bound into aware UTC, or ``None``.

    Date-only (all-day) values and unparseable values return ``None`` so the
    caller drops all-day events (spec §5.1).
    """
    if not isinstance(value, str) or _is_all_day(value):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        # The calendar API always returns aware datetimes; guard anyway so a
        # naive value is interpreted as local rather than silently as UTC.
        parsed = dt_util.as_local(parsed)
    return parsed.astimezone(UTC)


async def async_get_calendar_events(
    hass: HomeAssistant,
    entity_id: str,
    start_utc: datetime,
    end_utc: datetime,
    source_id: str,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Call ``calendar.get_events`` for the window.

    Returns ``(events, None)`` on success or ``(None, error_code)`` on
    failure, so the caller can emit a ``status="error"`` result instead of
    a misleading empty day (§5.3). An unavailable entity, a missing service
    or any raised error are distinct failure modes. Shared by the schedule and
    the lunch provider.
    """
    state = hass.states.get(entity_id)
    if state is None or state.state == "unavailable":
        _LOGGER.warning(
            "Calendar entity %s unavailable for source %s", entity_id, source_id
        )
        return None, ERROR_UNAVAILABLE

    try:
        response = await hass.services.async_call(
            CALENDAR_DOMAIN,
            SERVICE_GET_EVENTS,
            {
                "entity_id": entity_id,
                "start_date_time": start_utc,
                "end_date_time": end_utc,
            },
            blocking=True,
            return_response=True,
        )
    except HomeAssistantError as err:
        _LOGGER.warning(
            "calendar.get_events failed for source %s (%s): %s",
            source_id,
            entity_id,
            type(err).__name__,
        )
        return None, ERROR_SERVICE

    if not isinstance(response, dict):
        return None, ERROR_RESPONSE
    entity_data = response.get(entity_id)
    if not isinstance(entity_data, dict):
        return None, ERROR_RESPONSE
    events = entity_data.get("events")
    if not isinstance(events, list):
        return None, ERROR_RESPONSE
    return [event for event in events if isinstance(event, dict)], None


class HaCalendarScheduleProvider:
    """Fetches one local day's events from a HA calendar entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        source_filter: SourceFilter,
        source_id: str,
    ) -> None:
        self._hass = hass
        self._entity_id = entity_id
        self._filter = source_filter
        self._source_id = source_id

    @property
    def source_id(self) -> str:
        return self._source_id

    async def _async_get_events(
        self, start_utc: datetime, end_utc: datetime
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Call ``calendar.get_events`` for the window (see the module helper)."""
        return await async_get_calendar_events(
            self._hass, self._entity_id, start_utc, end_utc, self._source_id
        )

    async def async_get_day(
        self, d: date, *, now: datetime | None = None
    ) -> ScheduleResult:
        """Return the schedule for local date ``d`` (spec §5.3)."""
        fetched_at = now or datetime.now(UTC)
        start_utc, end_utc = local_day_bounds(d)
        raw, error_code = await self._async_get_events(start_utc, end_utc)
        if raw is None:
            return ScheduleResult(
                status="error",
                events=(),
                fetched_at=fetched_at,
                error_code=error_code,
                stale=False,
            )

        exclude = self._filter.exclude_patterns
        include = self._filter.include_patterns

        # Collect accepted events first, then sort by start so the
        # same-summary occurrence suffixes (-2, -3, …) follow start order
        # (plan §4.4).
        accepted: list[tuple[datetime, datetime, str, str | None, str | None]] = []
        for item in raw:
            raw_start = item.get("start")
            raw_end = item.get("end")
            # Drop all-day events (spec §5.1): they are tasks/holidays, not a
            # first lesson or work start.
            if _is_all_day(raw_start) or _is_all_day(raw_end):
                continue
            start = _parse_bound(raw_start)
            end = _parse_bound(raw_end)
            if start is None or end is None:
                continue
            summary = str(item.get("summary") or "")
            if exclude and _matches_any(summary, exclude):
                continue
            if include and not _matches_any(summary, include):
                continue
            location = item.get("location")
            description = item.get("description")
            accepted.append(
                (
                    start,
                    end,
                    summary,
                    str(location) if location is not None else None,
                    str(description) if description is not None else None,
                )
            )

        accepted.sort(key=lambda e: e[0])

        events: list[ScheduleEvent] = []
        summary_counts: dict[str, int] = {}
        for start, end, summary, location, description in accepted:
            # Raw DESCRIPTION is parsed for known override tags only and never
            # kept (plan §5.3, §10). LOCATION is retained raw (capped) for the
            # opt-in alias match and never geocoded.
            tags = parse_event_tags(summary, description)
            summary_key = strip_tags(summary).casefold()
            summary_counts[summary_key] = summary_counts.get(summary_key, 0) + 1
            events.append(
                ScheduleEvent(
                    uid=_event_uid(summary, start),
                    summary=summary,
                    start=start,
                    end=end,
                    source_id=self._source_id,
                    location=location,
                    tags=tags,
                    occurrence_key=_occurrence_key(
                        summary_key, summary_counts[summary_key]
                    ),
                )
            )

        return ScheduleResult(
            status="ok" if events else "empty",
            events=tuple(events),
            fetched_at=fetched_at,
            stale=False,
        )

    async def async_is_holiday(self, d: date) -> bool:
        """Return ``True`` when an all-day ``Ledig`` event covers date ``d``.

        Used for the shared household holiday calendar (spec §5.4). A fetch
        error returns ``False`` so a transient failure never fabricates a day
        off; schedule selection treats a source error separately.
        """
        start_utc, end_utc = local_day_bounds(d)
        raw, _error_code = await self._async_get_events(start_utc, end_utc)
        if raw is None:
            return False
        for item in raw:
            if not _is_all_day(item.get("start")):
                continue
            summary = str(item.get("summary") or "")
            if _matches_any(summary, (HOLIDAY_SUMMARY,)):
                return True
        return False


def _occurrence_key(summary_key: str, nth: int) -> str:
    """Stable per-occurrence key for a HA calendar event (plan §4.4, U8).

    ``get_events`` exposes no UID, so the key is ``sha1`` of the tag-stripped,
    casefolded summary, truncated to 10 hex characters. The ``nth`` same-summary
    event of the day (in start order) gets a ``-2``, ``-3``, … suffix. Moving a
    single event's time keeps its key (the day in the mission id separates
    days); renaming creates a new mission and drops the old one.

    Known limit of this UID-less API: the ``nth`` suffix follows start order, so
    moving one of several same-summary events past another swaps their keys and
    each mission inherits the other's ledger.
    """
    digest = hashlib.sha1(summary_key.encode()).hexdigest()[:10]
    if nth > 1:
        return f"{digest}-{nth}"
    return digest


def _event_uid(summary: str, start: datetime) -> str:
    """Synthesise a stable UID for a HA calendar event.

    ``calendar.get_events`` omits the UID from its list response, so a stable
    key is derived from the summary and UTC start. This is sufficient for
    first-event selection and disappearance detection within a single day. Two
    same-summary events starting together share this UID (their
    ``occurrence_key`` still differs), so disappearance detection cannot tell
    them apart.
    """
    return f"{start.isoformat()}|{summary}"
