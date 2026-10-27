"""Tests for the lunch-calendar provider (plan §7.2).

Runs against a real ``local_calendar`` entry, like the schedule provider tests,
so the all-day events a ``skolmat`` calendar produces are read through the real
``calendar.get_events`` action.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from custom_components.family_departures.providers.lunch_calendar import (
    MAX_DISH_LENGTH,
    MAX_DISHES,
    HaCalendarLunchProvider,
    split_dishes,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

NOW = datetime(2026, 10, 19, 18, 0, tzinfo=UTC)
TUESDAY = date(2026, 10, 20)
SATURDAY = date(2026, 10, 24)

LUNCH_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//lunch//EN
BEGIN:VEVENT
UID:lunch-tue
DTSTAMP:20261019T000000Z
DTSTART;VALUE=DATE:20261020
DTEND;VALUE=DATE:20261021
SUMMARY:Pasta bolognese | Sallad | Bröd
END:VEVENT
BEGIN:VEVENT
UID:lunch-wed
DTSTAMP:20261019T000000Z
DTSTART;VALUE=DATE:20261021
DTEND;VALUE=DATE:20261022
SUMMARY:Fiskgratäng\\nPotatis
END:VEVENT
END:VCALENDAR
"""


async def _setup_lunch_calendar(hass: HomeAssistant) -> str:
    path = Path(hass.config.path(".storage/local_calendar.skolmat.ics"))

    def _seed() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LUNCH_ICS)

    await hass.async_add_executor_job(_seed)
    assert await async_setup_component(hass, "calendar", {})
    entry = MockConfigEntry(
        domain="local_calendar",
        data={"calendar_name": "Skolmat", "storage_key": "skolmat"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return "calendar.skolmat"


def test_split_dishes_on_pipes_and_newlines_and_trims() -> None:
    assert split_dishes("Pasta | Sallad\n  Bröd  \n\n") == ["Pasta", "Sallad", "Bröd"]
    assert split_dishes("   ") == []


async def test_all_day_event_becomes_dishes(hass: HomeAssistant) -> None:
    entity_id = await _setup_lunch_calendar(hass)
    menu = await HaCalendarLunchProvider(hass, entity_id).async_get_lunch(
        TUESDAY, now=NOW
    )

    assert menu.status == "ok"
    assert menu.dishes == ("Pasta bolognese", "Sallad", "Bröd")
    assert menu.local_date == TUESDAY
    assert menu.fetched_at == NOW


async def test_newline_separated_summary(hass: HomeAssistant) -> None:
    entity_id = await _setup_lunch_calendar(hass)
    menu = await HaCalendarLunchProvider(hass, entity_id).async_get_lunch(
        date(2026, 10, 21), now=NOW
    )
    assert menu.dishes == ("Fiskgratäng", "Potatis")


async def test_weekend_without_event_is_empty(hass: HomeAssistant) -> None:
    entity_id = await _setup_lunch_calendar(hass)
    menu = await HaCalendarLunchProvider(hass, entity_id).async_get_lunch(
        SATURDAY, now=NOW
    )
    assert menu.status == "empty"
    assert menu.dishes == ()


async def test_missing_entity_is_an_error_not_an_empty_menu(
    hass: HomeAssistant,
) -> None:
    menu = await HaCalendarLunchProvider(hass, "calendar.nope").async_get_lunch(
        TUESDAY, now=NOW
    )
    assert menu.status == "error"
    assert menu.dishes == ()


def test_dish_count_and_length_are_capped() -> None:
    dishes = split_dishes(" | ".join("x" * 300 for _ in range(9)))
    assert len(dishes) == 9
    capped = tuple(d[:MAX_DISH_LENGTH] for d in dishes[:MAX_DISHES])
    assert len(capped) == MAX_DISHES
    assert all(len(d) == MAX_DISH_LENGTH for d in capped)
