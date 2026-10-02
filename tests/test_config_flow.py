"""Tests for the Family Departures config and options flows (spec §4.1-§4.3).

These exercise the household config flow and the profile/settings options
flow end to end with the PHACC ``hass`` fixture. The focus is product
behaviour: secrets land only in ``entry.data`` (never in ``entry.options`` or
error text), ICS validation surfaces connect/parse errors, SL margins are
offered even for a car-default profile, and an options change reloads the entry.
"""

from __future__ import annotations

from typing import Any

import pytest
from custom_components.family_departures.const import (
    DATA_HOME_LAT,
    DATA_HOME_LON,
    DATA_HOUSEHOLD_NAME,
    DATA_ICS_URLS,
    DOMAIN,
    OPT_DRY_RUN,
    OPT_PROFILES,
    OPT_TEST_RECIPIENT_SCRIPT,
)
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

ICS_URL = "https://feeds.example.test/rest-api/ical-feed/parent/SECRET-TOKEN-XYZ"

VALID_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:preview-1\r\n"
    "DTSTAMP:20261001T050000Z\r\n"
    "DTSTART;TZID=Europe/Stockholm:20261019T082000\r\n"
    "DTEND;TZID=Europe/Stockholm:20261019T091000\r\n"
    "SUMMARY:Lektion IDRO1000X\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


# ---------------------------------------------------------------------------
# Config (household) flow
# ---------------------------------------------------------------------------


async def test_user_flow_creates_household(hass: HomeAssistant) -> None:
    """The user step stores name and home coordinates, options start empty."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            DATA_HOUSEHOLD_NAME: "Familjen",
            "home_location": {"latitude": 59.33, "longitude": 18.06, "radius": 100},
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Familjen"
    assert result["data"][DATA_HOME_LAT] == 59.33
    assert result["data"][DATA_HOME_LON] == 18.06
    assert result["data"][DATA_ICS_URLS] == {}
    assert result["options"][OPT_PROFILES] == {}


async def test_single_instance_only(hass: HomeAssistant) -> None:
    """A second household flow aborts: only one entry is allowed."""
    MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data={}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Options flow helpers
# ---------------------------------------------------------------------------


def _household_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Familjen",
        data={
            DATA_HOUSEHOLD_NAME: "Familjen",
            DATA_HOME_LAT: 59.33,
            DATA_HOME_LON: 18.06,
            DATA_ICS_URLS: {},
        },
        options={OPT_PROFILES: {}},
    )


async def _setup_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = _household_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _open_add_profile(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Open the options menu, choose "add profile", return the flow id.

    The add-profile step has no form of its own (FEAT-001): it jumps straight
    to ``profile_source`` where the name is entered, so the id is derived from
    the name on save rather than preselected.
    """
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_profile"}
    )
    assert result["step_id"] == "profile_source"
    return str(result["flow_id"])


async def _submit_source(
    hass: HomeAssistant, flow_id: str, source_input: dict[str, Any]
) -> dict[str, Any]:
    """Submit the source step; return the next result."""
    return await hass.config_entries.options.async_configure(flow_id, source_input)


async def _complete_profile(
    hass: HomeAssistant,
    flow_id: str,
    *,
    default_mode: str = "car",
) -> dict[str, Any]:
    """Walk destination -> margins -> packing -> notifications -> test -> done."""
    result = await hass.config_entries.options.async_configure(
        flow_id,
        {
            "destination_location": {
                "latitude": 59.4,
                "longitude": 18.1,
                "radius": 50,
            },
            "default_mode": default_mode,
            "static_minutes": 15,
            "static_label": "Cykel",
            "weather_adjust": False,
            "car_fallback_minutes": 25,
        },
    )
    assert result["step_id"] == "profile_margins"

    result = await hass.config_entries.options.async_configure(
        flow_id,
        {
            "arrival": 0,
            "departure": 5,
            "boarding": 2,
            "parking_and_walk": 3,
            "min_transfer": 5,
            "weekday_mask": ["0", "1", "2", "3", "4"],
        },
    )
    assert result["step_id"] == "profile_packing"

    result = await hass.config_entries.options.async_configure(
        flow_id, {"packing_match": "", "packing_item": ""}
    )
    assert result["step_id"] == "profile_notifications"

    result = await hass.config_entries.options.async_configure(
        flow_id,
        {
            "quiet_start": "22:00:00",
            "quiet_end": "06:00:00",
            "notifications_enabled": False,
            "evening_notice_enabled": True,
            "change_threshold_minutes": 3,
        },
    )
    assert result["step_id"] == "profile_test"

    return await hass.config_entries.options.async_configure(flow_id, {})


# ---------------------------------------------------------------------------
# Options flow: profile happy path (HA calendar)
# ---------------------------------------------------------------------------


async def test_add_calendar_profile_happy_path(hass: HomeAssistant) -> None:
    """A full ha_calendar profile walk saves into options and reloads."""
    entry = await _setup_entry(hass)

    flow_id = await _open_add_profile(hass, entry)
    await _submit_source(
        hass,
        flow_id,
        {
            "name": "Parent B",
            "source_type": "ha_calendar",
            "calendar_entity_id": "calendar.parent_b",
        },
    )
    result = await _complete_profile(hass, flow_id, default_mode="car")
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    profiles = entry.options[OPT_PROFILES]
    # The id is the slug of the entered name (FEAT-001).
    assert "parent_b" in profiles
    parent_b = profiles["parent_b"]
    assert parent_b["source_type"] == "ha_calendar"
    assert parent_b["calendar_entity_id"] == "calendar.parent_b"
    assert parent_b["default_mode"] == "car"
    # SL/transit margins are stored even for a car-default profile (§4.3).
    assert parent_b["min_transfer"] == 5
    assert parent_b["boarding"] == 2
    # No ICS URL leaked into data for a calendar source.
    assert entry.data[DATA_ICS_URLS] == {}


async def test_test_calculation_shows_car_and_transit(hass: HomeAssistant) -> None:
    """Parent B's final step shows both a car and a transit plan (§4.3)."""
    entry = await _setup_entry(hass)
    flow_id = await _open_add_profile(hass, entry)
    await _submit_source(
        hass,
        flow_id,
        {
            "name": "Parent B",
            "source_type": "ha_calendar",
            "calendar_entity_id": "calendar.parent_b",
        },
    )
    # Walk up to the test step without submitting it.
    await hass.config_entries.options.async_configure(
        flow_id,
        {
            "destination_location": {
                "latitude": 59.4,
                "longitude": 18.1,
                "radius": 50,
            },
            "default_mode": "car",
            "weather_adjust": False,
        },
    )
    await hass.config_entries.options.async_configure(
        flow_id,
        {
            "arrival": 0,
            "departure": 5,
            "boarding": 2,
            "parking_and_walk": 3,
            "min_transfer": 5,
            "weekday_mask": ["0", "1", "2", "3", "4"],
        },
    )
    await hass.config_entries.options.async_configure(
        flow_id, {"packing_match": "", "packing_item": ""}
    )
    test_step = await hass.config_entries.options.async_configure(
        flow_id,
        {
            "quiet_start": "22:00:00",
            "quiet_end": "06:00:00",
            "notifications_enabled": False,
            "evening_notice_enabled": True,
            "change_threshold_minutes": 3,
        },
    )
    assert test_step["step_id"] == "profile_test"
    summary = test_step["description_placeholders"]["summary"]
    assert "Bil" in summary
    assert "Kollektivt" in summary


# ---------------------------------------------------------------------------
# Options flow: ICS source and secret handling
# ---------------------------------------------------------------------------


async def test_ics_profile_stores_url_in_data_only(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A valid ICS link validates, and the token lands only in entry.data."""
    aioclient_mock.get(ICS_URL, text=VALID_ICS)
    entry = await _setup_entry(hass)

    flow_id = await _open_add_profile(hass, entry)
    next_result = await _submit_source(
        hass,
        flow_id,
        {
            "name": "Kid B",
            "source_type": "ics",
            "ics_url": ICS_URL,
            "exclude_patterns": "LUNCH",
        },
    )
    assert next_result["step_id"] == "profile_destination"
    result = await _complete_profile(hass, flow_id, default_mode="car")
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    # The secret URL is in data, keyed by profile id, and nowhere in options.
    assert entry.data[DATA_ICS_URLS]["kid_b"] == ICS_URL
    assert ICS_URL not in str(entry.options)
    kid_b = entry.options[OPT_PROFILES]["kid_b"]
    assert "ics_url" not in kid_b
    assert kid_b["exclude_patterns"] == ["LUNCH"]


async def test_ics_connection_error_surfaced(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An HTTP 500 on the ICS link is reported without echoing the URL."""
    aioclient_mock.get(ICS_URL, status=500)
    entry = await _setup_entry(hass)

    flow_id = await _open_add_profile(hass, entry)
    bad = await _submit_source(
        hass,
        flow_id,
        {"name": "Kid B", "source_type": "ics", "ics_url": ICS_URL},
    )
    assert bad["type"] == FlowResultType.FORM
    assert bad["step_id"] == "profile_source"
    assert bad["errors"] == {"ics_url": "cannot_connect"}
    # The token must not appear in the error payload.
    assert ICS_URL not in str(bad)


async def test_ics_invalid_body_surfaced(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A non-calendar body is reported as an invalid calendar."""
    aioclient_mock.get(ICS_URL, text="this is not an ical document")
    entry = await _setup_entry(hass)

    flow_id = await _open_add_profile(hass, entry)
    bad = await _submit_source(
        hass,
        flow_id,
        {"name": "Kid B", "source_type": "ics", "ics_url": ICS_URL},
    )
    assert bad["type"] == FlowResultType.FORM
    assert bad["errors"] == {"ics_url": "invalid_calendar"}


async def test_ics_source_requires_url(hass: HomeAssistant) -> None:
    """Choosing ICS without a stored or entered URL is rejected."""
    entry = await _setup_entry(hass)
    flow_id = await _open_add_profile(hass, entry)
    bad = await _submit_source(
        hass,
        flow_id,
        {"name": "Kid A", "source_type": "ics"},
    )
    assert bad["errors"] == {"ics_url": "ics_required"}


async def test_calendar_source_requires_entity(hass: HomeAssistant) -> None:
    """Choosing ha_calendar without an entity is rejected."""
    entry = await _setup_entry(hass)
    flow_id = await _open_add_profile(hass, entry)
    bad = await _submit_source(
        hass,
        flow_id,
        {"name": "Parent B", "source_type": "ha_calendar"},
    )
    assert bad["errors"] == {"calendar_entity_id": "calendar_required"}


# ---------------------------------------------------------------------------
# Options flow: global settings and reload
# ---------------------------------------------------------------------------


async def test_settings_step_stores_dry_run_and_test_recipient(
    hass: HomeAssistant,
) -> None:
    """The settings step persists the dry-run flag and test recipient script."""
    entry = await _setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "settings"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {OPT_DRY_RUN: True, OPT_TEST_RECIPIENT_SCRIPT: "script.parent_a_test"},
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_DRY_RUN] is True
    assert entry.options[OPT_TEST_RECIPIENT_SCRIPT] == "script.parent_a_test"


async def test_options_change_reloads_entry(hass: HomeAssistant) -> None:
    """Finishing the settings step reloads the entry (spec §4.1)."""
    entry = await _setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED

    with pytest.MonkeyPatch.context():
        reloads: list[str] = []
        original = hass.config_entries.async_reload

        async def _tracking_reload(entry_id: str) -> bool:
            reloads.append(entry_id)
            return await original(entry_id)

        hass.config_entries.async_reload = _tracking_reload  # type: ignore[method-assign]

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "settings"}
        )
        await hass.config_entries.options.async_configure(
            result["flow_id"], {OPT_DRY_RUN: False}
        )
        await hass.async_block_till_done()

    assert entry.entry_id in reloads


# ---------------------------------------------------------------------------
# Options flow: user-defined profiles (slug id, add/remove, zero profiles)
# ---------------------------------------------------------------------------


async def _add_calendar_profile(
    hass: HomeAssistant, entry: MockConfigEntry, name: str
) -> str:
    """Add a full ha_calendar profile through the flow; return its stored id."""
    before = set(entry.options[OPT_PROFILES])
    flow_id = await _open_add_profile(hass, entry)
    await _submit_source(
        hass,
        flow_id,
        {
            "name": name,
            "source_type": "ha_calendar",
            "calendar_entity_id": "calendar.household",
        },
    )
    result = await _complete_profile(hass, flow_id, default_mode="car")
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    added = set(entry.options[OPT_PROFILES]) - before
    assert len(added) == 1
    return added.pop()


async def test_slug_id_generated_and_stable_across_rename(
    hass: HomeAssistant,
) -> None:
    """A profile's id is the slug of its name and never changes on rename (§9).

    A second profile with the same name gets a numeric suffix so ids stay
    unique and stable (FEAT-001 id generation).
    """
    entry = await _setup_entry(hass)

    # Add "Kid A" -> slug id "kid_a".
    first_id = await _add_calendar_profile(hass, entry, "Kid A")
    assert first_id == "kid_a"

    # Rename the profile to "Kiddo"; the id must stay "kid_a".
    flow_id = (await hass.config_entries.options.async_init(entry.entry_id))["flow_id"]
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "edit_profile"}
    )
    assert result["step_id"] == "edit_profile"
    source = await hass.config_entries.options.async_configure(
        flow_id, {"profile_id": "kid_a"}
    )
    assert source["step_id"] == "profile_source"
    await _submit_source(
        hass,
        flow_id,
        {
            "name": "Kiddo",
            "source_type": "ha_calendar",
            "calendar_entity_id": "calendar.household",
        },
    )
    result = await _complete_profile(hass, flow_id, default_mode="car")
    await hass.async_block_till_done()
    assert "kid_a" in entry.options[OPT_PROFILES]
    assert entry.options[OPT_PROFILES]["kid_a"]["name"] == "Kiddo"
    # No new id was created by the rename.
    assert set(entry.options[OPT_PROFILES]) == {"kid_a"}

    # Add a second profile also named "Kid A" -> id gets a numeric suffix.
    second_id = await _add_calendar_profile(hass, entry, "Kid A")
    assert second_id == "kid_a_2"
    assert set(entry.options[OPT_PROFILES]) == {"kid_a", "kid_a_2"}


async def test_add_then_remove_profile_cleans_entities(hass: HomeAssistant) -> None:
    """Adding then removing a profile purges its device and all its entities."""
    entry = await _setup_entry(hass)

    profile_id = await _add_calendar_profile(hass, entry, "Kid A")
    assert profile_id == "kid_a"

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    device_identifier = (DOMAIN, f"{entry.entry_id}_{profile_id}")

    # The profile's entities and device exist after it is added.
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    profile_entities = [
        e for e in entities if e.unique_id.startswith(f"{entry.entry_id}_{profile_id}_")
    ]
    assert profile_entities, "expected the new profile to have registered entities"
    devices = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    assert any(device_identifier in d.identifiers for d in devices)

    # Run the remove flow for that profile.
    flow_id = (await hass.config_entries.options.async_init(entry.entry_id))["flow_id"]
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "remove_profile"}
    )
    assert result["step_id"] == "remove_profile"
    result = await hass.config_entries.options.async_configure(
        flow_id, {"profile_id": profile_id}
    )
    assert result["step_id"] == "remove_confirm"
    result = await hass.config_entries.options.async_configure(flow_id, {})
    await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY

    # The profile is gone from options and from the secret ICS data map.
    assert profile_id not in entry.options[OPT_PROFILES]
    assert profile_id not in entry.data[DATA_ICS_URLS]

    # Its device and every one of its entities are gone (cascade).
    devices_after = dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    assert not any(device_identifier in d.identifiers for d in devices_after)
    remaining = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    assert not [
        e
        for e in remaining
        if e.unique_id.startswith(f"{entry.entry_id}_{profile_id}_")
    ]


async def test_zero_profiles_entry_loads_no_missions(hass: HomeAssistant) -> None:
    """An entry with no profiles sets up cleanly and plans nothing (§4.1)."""
    entry = await _setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.options[OPT_PROFILES] == {}

    coordinator = entry.runtime_data
    assert coordinator.data == {}
