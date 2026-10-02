"""Tests for the HA calendar schedule provider (spec §4.3, §5.3, §5.4).

These exercise the provider against a real ``local_calendar`` config entry so
the ``calendar.get_events`` round-trip, recurrence expansion, edited/deleted
occurrences and all-day handling are covered end to end. A missing entity is
asserted to be a ``status="error"``, never an empty day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from custom_components.family_departures.models import ScheduleResult, SourceFilter
from custom_components.family_departures.providers.ha_calendar import (
    ERROR_UNAVAILABLE,
    HaCalendarScheduleProvider,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import MockConfigEntry

FIXTURES = Path(__file__).parent / "fixtures" / "ics"
SOURCE_ID = "parent_b_local_calendar"
NOW = datetime(2026, 10, 19, 3, 0, tzinfo=UTC)


async def _setup_calendar(hass: HomeAssistant, name: str, ics: str) -> str:
    """Seed a local_calendar from ICS content and return its entity_id."""
    key = slugify(name)
    path = Path(hass.config.path(f".storage/local_calendar.{key}.ics"))

    def _seed() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ics)

    await hass.async_add_executor_job(_seed)

    assert await async_setup_component(hass, "calendar", {})
    entry = MockConfigEntry(
        domain="local_calendar",
        data={"calendar_name": name, "storage_key": key},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return f"calendar.{key}"


@pytest.fixture
async def parent_b_calendar(hass: HomeAssistant) -> str:
    ics = (FIXTURES / "local_calendar_parent.ics").read_text()
    return await _setup_calendar(hass, "Parent B", ics)


def _provider(hass: HomeAssistant, entity_id: str) -> HaCalendarScheduleProvider:
    return HaCalendarScheduleProvider(hass, entity_id, SourceFilter(), SOURCE_ID)


async def test_recurring_event_expanded(
    hass: HomeAssistant, parent_b_calendar: str
) -> None:
    """A weekday recurring work event resolves to the right local day."""
    provider = _provider(hass, parent_b_calendar)
    result = await provider.async_get_day(date(2026, 10, 20), now=NOW)

    assert result.status == "ok"
    assert len(result.events) == 1
    # 08:00 Stockholm (CEST, +02:00) on 2026-10-20 == 06:00 UTC.
    assert result.events[0].start == datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
    assert result.events[0].summary == "Jobb"
    assert result.events[0].source_id == SOURCE_ID


async def test_edited_single_occurrence(
    hass: HomeAssistant, parent_b_calendar: str
) -> None:
    """A RECURRENCE-ID override shifts just that day's start."""
    provider = _provider(hass, parent_b_calendar)
    result = await provider.async_get_day(date(2026, 10, 21), now=NOW)

    assert result.status == "ok"
    assert len(result.events) == 1
    # Moved to 09:30 Stockholm == 07:30 UTC.
    assert result.events[0].start == datetime(2026, 10, 21, 7, 30, tzinfo=UTC)
    assert result.events[0].summary == "Jobb senare start"


async def test_deleted_occurrence_is_empty(
    hass: HomeAssistant, parent_b_calendar: str
) -> None:
    """An EXDATE removes the occurrence; the expected day is empty, not errored."""
    provider = _provider(hass, parent_b_calendar)
    result = await provider.async_get_day(date(2026, 10, 22), now=NOW)

    assert result.status == "empty"
    assert result.events == ()
    assert result.error_code is None


async def test_all_day_event_dropped(
    hass: HomeAssistant, parent_b_calendar: str
) -> None:
    """An all-day 'Ledig' event is not treated as a first lesson."""
    provider = _provider(hass, parent_b_calendar)
    # 2026-10-24 is a Saturday with only the all-day event.
    result = await provider.async_get_day(date(2026, 10, 24), now=NOW)

    assert result.status == "empty"
    assert result.events == ()


async def test_holiday_detection(hass: HomeAssistant, parent_b_calendar: str) -> None:
    """async_is_holiday finds the all-day 'Ledig' event."""
    provider = _provider(hass, parent_b_calendar)
    assert await provider.async_is_holiday(date(2026, 10, 24)) is True
    assert await provider.async_is_holiday(date(2026, 10, 20)) is False


async def test_exclude_filter_applied(
    hass: HomeAssistant, parent_b_calendar: str
) -> None:
    """An exclude pattern drops matching summaries case-insensitively."""
    provider = HaCalendarScheduleProvider(
        hass, parent_b_calendar, SourceFilter(exclude_patterns=("jobb",)), SOURCE_ID
    )
    result = await provider.async_get_day(date(2026, 10, 20), now=NOW)

    assert result.status == "empty"
    assert result.events == ()


async def test_missing_entity_is_error_not_empty(hass: HomeAssistant) -> None:
    """A missing calendar entity is a source error, never a confirmed day off."""
    assert await async_setup_component(hass, "calendar", {})
    provider = _provider(hass, "calendar.does_not_exist")
    result = await provider.async_get_day(date(2026, 10, 20), now=NOW)

    assert isinstance(result, ScheduleResult)
    assert result.status == "error"
    assert result.error_code == ERROR_UNAVAILABLE
    assert result.events == ()


async def test_fetched_at_defaults_to_now(
    hass: HomeAssistant, parent_b_calendar: str
) -> None:
    """When no now is passed, fetched_at is an aware timestamp."""
    provider = _provider(hass, parent_b_calendar)
    result = await provider.async_get_day(date(2026, 10, 20))
    assert result.fetched_at.tzinfo is not None
