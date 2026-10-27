"""Tests for the Family Departures config and options flows (spec §4.1-§4.3).

These exercise the household config flow and the profile/settings options
flow end to end with the PHACC ``hass`` fixture. The focus is product
behaviour: secrets land only in ``entry.data`` (never in ``entry.options`` or
error text), ICS validation surfaces connect/parse errors, SL margins are
offered even for a car-default profile, and an options change reloads the entry.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
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
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
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


def _assert_schema_renderable(schema: Any) -> None:
    """Every field must serialize to something the frontend can render.

    A bare Python ``str`` type serializes to ``cv.UNSUPPORTED`` via
    ``voluptuous-serialize``; the frontend then draws an empty dialog with a
    dead Submit button. Each field must instead carry a selector.
    """
    for key, value in schema.schema.items():
        serialized = cv.custom_serializer(value)
        assert serialized is not cv.UNSUPPORTED, (
            f"field {key} serializes to UNSUPPORTED and will not render"
        )


def _assert_location_fields_have_initial_value(schema: Any) -> None:
    """A `location` selector must carry an initial value or the frontend crashes.

    This models the exact failure condition from the HA frontend's
    ``compute-initial-ha-form-data.ts`` / ``get-selector-initial-value.ts``:
    for a ``location`` selector, ``getSelectorInitialValue`` throws unless the
    field short-circuits first with either a non-null ``suggested_value`` or a
    ``default``. A bare required location selector therefore crashes the dialog
    before it renders (orphaning the flow -> ``already_in_progress`` on retry).
    """
    for key, value in schema.schema.items():
        if not isinstance(value, selector.LocationSelector):
            continue
        has_default = getattr(key, "default", vol.UNDEFINED) is not vol.UNDEFINED
        description = getattr(key, "description", None)
        has_suggested = (
            isinstance(description, dict)
            and description.get("suggested_value") is not None
        )
        assert has_default or has_suggested, (
            f"location field {key} has neither a default nor a non-null "
            f"suggested_value and will crash the HA frontend before render"
        )


async def test_user_form_schema_is_frontend_renderable(hass: HomeAssistant) -> None:
    """The user step's schema must render in the frontend (no UNSUPPORTED fields).

    This guards the empty-form / dead-Submit regression: a bare ``str`` field
    serializes to ``UNSUPPORTED`` and the dialog shows no inputs. It fails
    against the pre-fix schema and passes once ``household_name`` is a selector.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    schema = result["data_schema"]
    _assert_schema_renderable(schema)
    # household_name specifically must now carry a text selector.
    serialized = {
        str(key): cv.custom_serializer(value) for key, value in schema.schema.items()
    }
    household = serialized[DATA_HOUSEHOLD_NAME]
    assert isinstance(household, dict)
    assert "selector" in household
    assert "text" in household["selector"]


async def test_user_form_location_has_initial_value(hass: HomeAssistant) -> None:
    """The user step's `home_location` must carry an initial value.

    A bare required ``location`` selector crashes the HA frontend before the
    dialog renders (the reported "Submit does nothing" / "already in progress"
    bug). This fails against the pre-fix schema and passes once the field
    carries a suggested value from the HA home coordinates.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.FORM
    _assert_location_fields_have_initial_value(result["data_schema"])


# ---------------------------------------------------------------------------
# Options flow helpers
# ---------------------------------------------------------------------------

SCHOOL = {
    "id": "school",
    "name": "Skolan",
    "lat": 59.4,
    "lon": 18.1,
    "aliases": [],
    "static_minutes": None,
}
HALL = {
    "id": "hall",
    "name": "Sporthallen",
    "lat": 59.5,
    "lon": 18.2,
    "aliases": ["Idrottshallen"],
    "static_minutes": None,
}
SCHOOL_CAL = {
    "id": "school_cal",
    "name": "Skolan",
    "source_type": "ha_calendar",
    "calendar_entity_id": "calendar.school",
}
CLUB_CAL = {
    "id": "club_cal",
    "name": "Klubben",
    "source_type": "ha_calendar",
    "calendar_entity_id": "calendar.club",
}


def _link(link_id: str, calendar_id: str, role: str, **extra: Any) -> dict[str, Any]:
    return {
        "link_id": link_id,
        "calendar_id": calendar_id,
        "role": role,
        "label": "",
        "include_patterns": [],
        "exclude_patterns": [],
        "attendee_mode": "all",
        "location_id": None,
        "mode": None,
        "static_minutes": None,
        "arrival_margin": None,
        "rules": [],
        "match_event_location": False,
        "show_summary": False,
        **extra,
    }


def _profile_v2(
    pid: str,
    *,
    person: str | None = None,
    links: tuple[dict[str, Any], ...] = (),
    copy_to: tuple[str, ...] = (),
    location: str = "school",
) -> dict[str, Any]:
    return {
        "id": pid,
        "name": pid.title(),
        "is_adult": False,
        "person_entity_id": person,
        "default_location_id": location,
        "default_mode": "car",
        "static_minutes": None,
        "static_label": None,
        "weather_adjust": False,
        "car_fallback_minutes": None,
        "arrival": 5,
        "departure": 5,
        "boarding": 2,
        "parking_and_walk": 3,
        "min_transfer": 5,
        "weekday_mask": ["0", "1", "2", "3", "4"],
        "packing_rules": [],
        "notifications_enabled": False,
        "evening_notice_enabled": True,
        "change_threshold_minutes": 3,
        "quiet_start": "22:00:00",
        "quiet_end": "06:00:00",
        "scripts": {},
        "calendar_links": list(links),
        "copy_to": list(copy_to),
        "copy_kinds": ["cleanup", "leave_now"],
        "lunch_calendar_entity_id": None,
        "lunch_in_digest": True,
    }


def _household_entry(
    *,
    profiles: dict[str, Any] | None = None,
    locations: dict[str, Any] | None = None,
    calendars: dict[str, Any] | None = None,
    ics_urls: dict[str, str] | None = None,
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Familjen",
        version=2,
        data={
            DATA_HOUSEHOLD_NAME: "Familjen",
            DATA_HOME_LAT: 59.33,
            DATA_HOME_LON: 18.06,
            DATA_ICS_URLS: ics_urls or {},
        },
        options={
            OPT_PROFILES: profiles or {},
            "locations": locations or {},
            "calendars": calendars or {},
        },
    )


async def _setup_entry(hass: HomeAssistant, **seed: Any) -> MockConfigEntry:
    entry = _household_entry(**seed)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _open(hass: HomeAssistant, entry: MockConfigEntry, *path: str) -> dict:
    """Open the options menu and follow ``path`` of menu choices."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    for step in path:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    return result


async def _submit(
    hass: HomeAssistant, result: dict, data: dict[str, Any] | None = None
) -> dict:
    return await hass.config_entries.options.async_configure(
        result["flow_id"], data or {}
    )


LINK_FIELDS: dict[str, Any] = {
    "label": "",
    "include_patterns": "",
    "exclude_patterns": "",
    "location": "profile-default",
    "mode": "profile-default",
    "rules": "",
    "match_event_location": False,
    "show_summary": False,
}


async def _walk_profile(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    person: str = "person.kid_a",
    name: str = "Kid A",
    new_location: str | None = "Skolan",
    location: str | None = None,
    calendars: list[str] | None = None,
    main: str | None = None,
    links: dict[str, dict[str, Any]] | None = None,
    notifications: dict[str, Any] | None = None,
    finish: bool = True,
    start: dict | None = None,
) -> dict:
    """Walk the add-profile flow; return the result at (or after) ``profile_test``."""
    result = start or await _open(hass, entry, "add_profile")
    assert result["step_id"] == "profile_person"
    result = await _submit(
        hass, result, {"person_entity_id": person, "name": name, "is_adult": False}
    )
    assert result["step_id"] == "profile_destination", result.get("errors")
    dest: dict[str, Any] = {"default_mode": "car", "weather_adjust": False}
    if new_location:
        dest["new_location_name"] = new_location
        dest["destination_location"] = {
            "latitude": 59.4,
            "longitude": 18.1,
            "radius": 50,
        }
    if location:
        dest["default_location"] = location
    result = await _submit(hass, result, dest)
    assert result["step_id"] == "profile_calendars", result.get("errors")
    cal_input: dict[str, Any] = {"calendars": calendars or []}
    if main:
        cal_input["main_calendar"] = main
    result = await _submit(hass, result, cal_input)
    for calendar_id in calendars or []:
        assert result["step_id"] == "profile_link", result.get("errors")
        result = await _submit(
            hass, result, {**LINK_FIELDS, **(links or {}).get(calendar_id, {})}
        )
    assert result["step_id"] == "profile_margins"
    result = await _submit(
        hass,
        result,
        {
            "is_adult": False,
            "arrival": 0,
            "departure": 5,
            "boarding": 2,
            "parking_and_walk": 3,
            "min_transfer": 5,
            "weekday_mask": ["0", "1", "2", "3", "4"],
        },
    )
    assert result["step_id"] == "profile_packing"
    result = await _submit(hass, result, {"packing_match": "", "packing_item": ""})
    assert result["step_id"] == "profile_notifications"
    result = await _submit(
        hass,
        result,
        {
            "quiet_start": "22:00:00",
            "quiet_end": "06:00:00",
            "notifications_enabled": False,
            "evening_notice_enabled": True,
            "change_threshold_minutes": 3,
            **(notifications or {}),
        },
    )
    assert result["step_id"] == "profile_test"
    if not finish:
        return result
    result = await _submit(hass, result)
    await hass.async_block_till_done()
    return result


def _suggested(schema: Any, field: str) -> Any:
    for key in schema.schema:
        if str(key) == field:
            suggested = (key.description or {}).get("suggested_value")
            if suggested is not None:
                return suggested
            return key.default()
    raise AssertionError(f"no field {field!r}")


# ---------------------------------------------------------------------------
# Profiles: add, stored shape, test calculation
# ---------------------------------------------------------------------------


async def test_add_profile_with_new_location_main_and_extra_link_and_rules(
    hass: HomeAssistant,
) -> None:
    """A profile with a person, an inline new location, a main and an extra link."""
    entry = await _setup_entry(
        hass,
        locations={"hall": HALL},
        calendars={"school_cal": SCHOOL_CAL, "club_cal": CLUB_CAL},
    )
    result = await _walk_profile(
        hass,
        entry,
        calendars=["school_cal", "club_cal"],
        main="school_cal",
        links={
            "school_cal": {"exclude_patterns": "LUNCH", "label": "Skolan"},
            "club_cal": {
                "label": "Fotboll",
                "location": "hall",
                "mode": "public_transport",
                "rules": "Simhall => Sporthallen, kollektivt",
                "show_summary": True,
            },
        },
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    profile = entry.options[OPT_PROFILES]["kid_a"]
    assert profile["person_entity_id"] == "person.kid_a"
    assert profile["default_location_id"] == "skolan"
    assert entry.options["locations"]["skolan"]["lat"] == 59.4
    main, extra = profile["calendar_links"]
    assert (main["role"], main["calendar_id"]) == ("main", "school_cal")
    assert main["exclude_patterns"] == ["LUNCH"]
    assert (extra["role"], extra["calendar_id"]) == ("extra", "club_cal")
    assert extra["location_id"] == "hall"
    assert extra["mode"] == "public_transport"
    assert extra["show_summary"] is True
    assert extra["rules"] == [
        {"match": "Simhall", "location_id": "hall", "mode": "public_transport"}
    ]
    # SL/transit margins are stored even for a car-default profile (§4.3).
    assert profile["min_transfer"] == 5 and profile["boarding"] == 2


async def test_profile_uses_a_saved_location_and_defaults_the_name(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass, locations={"hall": HALL})
    hass.states.async_set("person.kid_a", "home", {"friendly_name": "Barn A"})

    result = await _walk_profile(
        hass, entry, name="", new_location=None, location="hall"
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    profile = entry.options[OPT_PROFILES]["barn_a"]
    assert profile["name"] == "Barn A"
    assert profile["default_location_id"] == "hall"
    assert set(entry.options["locations"]) == {"hall"}


async def test_zero_link_profile_saves(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)
    result = await _walk_profile(hass, entry, calendars=[])
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_PROFILES]["kid_a"]["calendar_links"] == []


async def test_test_calculation_shows_car_transit_and_each_link(
    hass: HomeAssistant,
) -> None:
    """The final step shows a car and a transit plan and one line per link (§4.3)."""
    entry = await _setup_entry(
        hass, calendars={"school_cal": SCHOOL_CAL, "club_cal": CLUB_CAL}
    )
    result = await _walk_profile(
        hass,
        entry,
        calendars=["school_cal", "club_cal"],
        main="school_cal",
        finish=False,
    )
    summary = result["description_placeholders"]["summary"]
    assert "Bil" in summary
    assert "Kollektivt" in summary
    assert "(huvud)" in summary and "(extra)" in summary


async def test_profile_validation_errors(hass: HomeAssistant) -> None:
    entry = await _setup_entry(
        hass,
        profiles={"kid_b": _profile_v2("kid_b", person="person.kid_b")},
        locations={"school": SCHOOL, "hall": HALL},
        calendars={"school_cal": SCHOOL_CAL, "club_cal": CLUB_CAL},
    )

    # A person belongs to one profile only (the field itself is required).
    person = await _open(hass, entry, "add_profile")
    taken = await _submit(
        hass, person, {"person_entity_id": "person.kid_b", "is_adult": False}
    )
    assert taken["errors"] == {"person_entity_id": "person_in_use"}

    # A location is needed: a saved one or a new, named one.
    dest = await _submit(
        hass,
        person,
        {"person_entity_id": "person.kid_a", "name": "Kid A", "is_adult": False},
    )
    assert dest["step_id"] == "profile_destination"
    no_location = await _submit(
        hass, dest, {"default_mode": "car", "weather_adjust": False}
    )
    assert no_location["errors"] == {"default_location": "location_required"}

    # The main calendar must be one of the linked ones.
    cals = await _submit(
        hass,
        dest,
        {"default_location": "school", "default_mode": "car", "weather_adjust": False},
    )
    assert cals["step_id"] == "profile_calendars"
    bad_main = await _submit(
        hass, cals, {"calendars": ["club_cal"], "main_calendar": "school_cal"}
    )
    assert bad_main["errors"] == {"main_calendar": "main_not_linked"}

    # Unknown rule tokens are rejected on the link step.
    link = await _submit(hass, cals, {"calendars": ["club_cal"]})
    assert link["step_id"] == "profile_link"
    bad_rules = await _submit(
        hass, link, {**LINK_FIELDS, "rules": "Simhall => Okänd plats"}
    )
    assert bad_rules["errors"] == {"rules": "rules_invalid"}
    ok = await _submit(hass, link, {**LINK_FIELDS, "rules": "Simhall => Sporthallen"})
    assert ok["step_id"] == "profile_margins"


async def test_inline_new_calendar_duplicate_of_a_linked_one_is_rejected(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(
        hass,
        locations={"school": SCHOOL},
        calendars={"school_cal": SCHOOL_CAL},
    )
    person = await _open(hass, entry, "add_profile")
    dest = await _submit(
        hass,
        person,
        {"person_entity_id": "person.kid_a", "name": "Kid A", "is_adult": False},
    )
    cals = await _submit(
        hass,
        dest,
        {"default_location": "school", "default_mode": "car", "weather_adjust": False},
    )
    bad = await _submit(
        hass,
        cals,
        {
            "calendars": ["school_cal"],
            "new_calendar_name": "Skolan igen",
            "new_calendar_source_type": "ha_calendar",
            "new_calendar_entity_id": "calendar.school",
        },
    )
    assert bad["errors"] == {"new_calendar_name": "duplicate_link"}


async def test_inline_new_ics_calendar_is_saved_with_the_profile(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(ICS_URL, text=VALID_ICS)
    entry = await _setup_entry(hass, locations={"school": SCHOOL})
    person = await _open(hass, entry, "add_profile")
    dest = await _submit(
        hass,
        person,
        {"person_entity_id": "person.kid_a", "name": "Kid A", "is_adult": False},
    )
    cals = await _submit(
        hass,
        dest,
        {"default_location": "school", "default_mode": "car", "weather_adjust": False},
    )
    link = await _submit(
        hass,
        cals,
        {
            "calendars": [],
            "main_calendar": "new-calendar",
            "new_calendar_name": "Skolschema",
            "new_calendar_source_type": "ics",
            "new_calendar_ics_url": ICS_URL,
        },
    )
    assert link["step_id"] == "profile_link"
    await _submit(hass, link, LINK_FIELDS)  # then margins ... (finish below)
    # Resume at margins: finish the remaining steps.
    flow = hass.config_entries.options
    flow_id = link["flow_id"]
    await flow.async_configure(
        flow_id,
        {
            "is_adult": False,
            "arrival": 0,
            "departure": 5,
            "boarding": 2,
            "parking_and_walk": 3,
            "min_transfer": 5,
            "weekday_mask": ["0"],
        },
    )
    await flow.async_configure(flow_id, {"packing_match": "", "packing_item": ""})
    await flow.async_configure(
        flow_id,
        {
            "quiet_start": "22:00:00",
            "quiet_end": "06:00:00",
            "notifications_enabled": False,
            "evening_notice_enabled": True,
            "change_threshold_minutes": 3,
        },
    )
    done = await flow.async_configure(flow_id, {})
    await hass.async_block_till_done()

    assert done["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["calendars"]["skolschema"]["source_type"] == "ics"
    assert entry.data[DATA_ICS_URLS]["skolschema"] == ICS_URL
    assert ICS_URL not in str(entry.options)
    [saved_link] = entry.options[OPT_PROFILES]["kid_a"]["calendar_links"]
    assert (saved_link["role"], saved_link["calendar_id"]) == ("main", "skolschema")


# ---------------------------------------------------------------------------
# Locations registry
# ---------------------------------------------------------------------------


async def test_add_edit_and_remove_a_location(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)

    form = await _open(hass, entry, "locations", "location_add")
    assert form["step_id"] == "location_form"
    _assert_schema_renderable(form["data_schema"])
    done = await _submit(
        hass,
        form,
        {
            "name": "Sporthallen",
            "location": {"latitude": 59.5, "longitude": 18.2, "radius": 50},
            "aliases": "Idrottshallen, Hallen",
            "static_minutes": 12,
        },
    )
    await hass.async_block_till_done()
    assert done["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["locations"]["sporthallen"] == {
        "id": "sporthallen",
        "name": "Sporthallen",
        "lat": 59.5,
        "lon": 18.2,
        "aliases": ["Idrottshallen", "Hallen"],
        "static_minutes": 12,
    }

    pick = await _open(hass, entry, "locations", "location_edit")
    form = await _submit(hass, pick, {"location_id": "sporthallen"})
    assert _suggested(form["data_schema"], "location")["latitude"] == 59.5
    await _submit(
        hass,
        form,
        {
            "name": "Stora hallen",
            "location": {"latitude": 59.5, "longitude": 18.2, "radius": 50},
            "aliases": "",
        },
    )
    await hass.async_block_till_done()
    assert entry.options["locations"]["sporthallen"]["name"] == "Stora hallen"
    assert set(entry.options["locations"]) == {"sporthallen"}

    pick = await _open(hass, entry, "locations", "location_remove")
    done = await _submit(hass, pick, {"location_id": "sporthallen"})
    await hass.async_block_till_done()
    assert done["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["locations"] == {}


async def test_location_in_use_cannot_be_removed(hass: HomeAssistant) -> None:
    link = _link("club", "club_cal", "extra", location_id="hall")
    entry = await _setup_entry(
        hass,
        profiles={
            "kid_a": _profile_v2("kid_a", links=(link,)),
        },
        locations={"school": SCHOOL, "hall": HALL},
        calendars={"club_cal": CLUB_CAL},
    )
    for location_id in ("school", "hall"):  # profile default, then a link's
        pick = await _open(hass, entry, "locations", "location_remove")
        bad = await _submit(hass, pick, {"location_id": location_id})
        assert bad["type"] == FlowResultType.FORM
        assert bad["errors"] == {"location_id": "location_in_use"}
    assert set(entry.options["locations"]) == {"school", "hall"}


# ---------------------------------------------------------------------------
# Calendars registry and secret handling
# ---------------------------------------------------------------------------


async def test_add_calendar_with_two_linked_persons_gives_both_extra_links(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(
        hass,
        profiles={
            "kid_a": _profile_v2("kid_a", person="person.kid_a"),
            "kid_b": _profile_v2("kid_b", person="person.kid_b"),
            "kid_c": _profile_v2("kid_c", person="person.kid_c"),
        },
        locations={"school": SCHOOL},
    )
    form = await _open(hass, entry, "calendars", "calendar_add")
    _assert_schema_renderable(form["data_schema"])
    done = await _submit(
        hass,
        form,
        {
            "name": "Fotboll",
            "source_type": "ha_calendar",
            "calendar_entity_id": "calendar.football",
            "persons": ["kid_a", "kid_b"],
        },
    )
    await hass.async_block_till_done()

    assert done["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options["calendars"]["fotboll"]["calendar_entity_id"] == (
        "calendar.football"
    )
    profiles = entry.options[OPT_PROFILES]
    for pid in ("kid_a", "kid_b"):
        [link] = profiles[pid]["calendar_links"]
        assert (link["role"], link["calendar_id"]) == ("extra", "fotboll")
        # Shared by two persons: each sees untagged events and those naming them.
        assert link["attendee_mode"] == "tagged_or_untagged"
    assert profiles["kid_c"]["calendar_links"] == []


async def test_editing_a_calendar_relinks_persons(hass: HomeAssistant) -> None:
    link = _link("club_cal", "club_cal", "extra")
    entry = await _setup_entry(
        hass,
        profiles={
            "kid_a": _profile_v2("kid_a", links=(link,)),
            "kid_b": _profile_v2("kid_b"),
        },
        locations={"school": SCHOOL},
        calendars={"club_cal": CLUB_CAL},
    )
    pick = await _open(hass, entry, "calendars", "calendar_edit")
    form = await _submit(hass, pick, {"calendar_id": "club_cal"})
    assert _suggested(form["data_schema"], "persons") == ["kid_a"]
    await _submit(
        hass,
        form,
        {
            "name": "Klubben",
            "source_type": "ha_calendar",
            "calendar_entity_id": "calendar.club",
            "persons": ["kid_b"],
        },
    )
    await hass.async_block_till_done()

    profiles = entry.options[OPT_PROFILES]
    assert profiles["kid_a"]["calendar_links"] == []
    assert profiles["kid_b"]["calendar_links"][0]["calendar_id"] == "club_cal"


async def test_ics_calendar_stores_url_in_data_only(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A valid ICS link validates, and the token lands only in entry.data."""
    aioclient_mock.get(ICS_URL, text=VALID_ICS)
    entry = await _setup_entry(hass)

    form = await _open(hass, entry, "calendars", "calendar_add")
    done = await _submit(
        hass,
        form,
        {"name": "Klubben", "source_type": "ics", "ics_url": ICS_URL},
    )
    await hass.async_block_till_done()

    assert done["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[DATA_ICS_URLS] == {"klubben": ICS_URL}
    assert ICS_URL not in str(entry.options)
    assert "ics_url" not in str(entry.options["calendars"])


async def test_blank_url_on_edit_keeps_the_stored_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(ICS_URL, text=VALID_ICS)
    ics_cal = {"id": "klubben", "name": "Klubben", "source_type": "ics"}
    entry = await _setup_entry(
        hass, calendars={"klubben": ics_cal}, ics_urls={"klubben": ICS_URL}
    )
    pick = await _open(hass, entry, "calendars", "calendar_edit")
    form = await _submit(hass, pick, {"calendar_id": "klubben"})
    await _submit(hass, form, {"name": "Klubben 2", "source_type": "ics"})
    await hass.async_block_till_done()

    assert entry.data[DATA_ICS_URLS] == {"klubben": ICS_URL}
    assert entry.options["calendars"]["klubben"]["name"] == "Klubben 2"


async def test_ics_connection_error_surfaced(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An HTTP 500 on the ICS link is reported without echoing the URL."""
    aioclient_mock.get(ICS_URL, status=500)
    entry = await _setup_entry(hass)

    form = await _open(hass, entry, "calendars", "calendar_add")
    bad = await _submit(
        hass, form, {"name": "Klubben", "source_type": "ics", "ics_url": ICS_URL}
    )
    assert bad["type"] == FlowResultType.FORM
    assert bad["step_id"] == "calendar_form"
    assert bad["errors"] == {"ics_url": "cannot_connect"}
    # The token must not appear in the error payload.
    assert ICS_URL not in str(bad)


async def test_ics_invalid_body_surfaced(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A non-calendar body is reported as an invalid calendar."""
    aioclient_mock.get(ICS_URL, text="this is not an ical document")
    entry = await _setup_entry(hass)

    form = await _open(hass, entry, "calendars", "calendar_add")
    bad = await _submit(
        hass, form, {"name": "Klubben", "source_type": "ics", "ics_url": ICS_URL}
    )
    assert bad["errors"] == {"ics_url": "invalid_calendar"}
    assert ICS_URL not in str(bad)


async def test_calendar_source_requirements(hass: HomeAssistant) -> None:
    entry = await _setup_entry(hass)
    form = await _open(hass, entry, "calendars", "calendar_add")
    no_url = await _submit(hass, form, {"name": "A", "source_type": "ics"})
    assert no_url["errors"] == {"ics_url": "ics_required"}
    no_entity = await _submit(hass, form, {"name": "B", "source_type": "ha_calendar"})
    assert no_entity["errors"] == {"calendar_entity_id": "calendar_required"}


async def test_removing_a_calendar_removes_its_links_and_url(
    hass: HomeAssistant,
) -> None:
    secret = "https://feeds.example.test/secret-token"
    ics_cal = {"id": "klubben", "name": "Klubben", "source_type": "ics"}
    link = _link("klubben", "klubben", "extra")
    entry = await _setup_entry(
        hass,
        profiles={
            "kid_a": _profile_v2("kid_a", links=(link,)),
            "kid_b": _profile_v2("kid_b", links=(link,)),
        },
        locations={"school": SCHOOL},
        calendars={"klubben": ics_cal, "school_cal": SCHOOL_CAL},
        ics_urls={"klubben": secret},
    )
    pick = await _open(hass, entry, "calendars", "calendar_remove")
    confirm = await _submit(hass, pick, {"calendar_id": "klubben"})
    assert confirm["step_id"] == "calendar_remove_confirm"
    done = await _submit(hass, confirm)
    await hass.async_block_till_done()

    assert done["type"] == FlowResultType.CREATE_ENTRY
    assert set(entry.options["calendars"]) == {"school_cal"}
    assert entry.data[DATA_ICS_URLS] == {}
    assert secret not in str(entry.data) + str(entry.options)
    for pid in ("kid_a", "kid_b"):
        assert entry.options[OPT_PROFILES][pid]["calendar_links"] == []


# ---------------------------------------------------------------------------
# Global settings and reload
# ---------------------------------------------------------------------------


async def test_settings_step_stores_dry_run_and_test_recipient(
    hass: HomeAssistant,
) -> None:
    """The settings step persists the dry-run flag and test recipient script."""
    entry = await _setup_entry(hass)
    result = await _open(hass, entry, "settings")
    assert result["step_id"] == "settings"
    result = await _submit(
        hass,
        result,
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

    reloads: list[str] = []
    original = hass.config_entries.async_reload

    async def _tracking_reload(entry_id: str) -> bool:
        reloads.append(entry_id)
        return await original(entry_id)

    hass.config_entries.async_reload = _tracking_reload  # type: ignore[method-assign]
    result = await _open(hass, entry, "settings")
    await _submit(hass, result, {OPT_DRY_RUN: False})
    await hass.async_block_till_done()

    assert entry.entry_id in reloads


# ---------------------------------------------------------------------------
# Profile ids, editing, removal
# ---------------------------------------------------------------------------


async def test_slug_id_generated_and_stable_across_rename(
    hass: HomeAssistant,
) -> None:
    """A profile's id is the slug of its name and never changes on rename (§9).

    A second profile with the same name gets a numeric suffix so ids stay
    unique and stable (FEAT-001 id generation).
    """
    entry = await _setup_entry(hass, locations={"school": SCHOOL})
    await _walk_profile(hass, entry, new_location=None, location="school")
    assert set(entry.options[OPT_PROFILES]) == {"kid_a"}

    # Rename the profile; the id must stay "kid_a".
    pick = await _open(hass, entry, "edit_profile")
    person = await _submit(hass, pick, {"profile_id": "kid_a"})
    assert person["step_id"] == "profile_person"
    assert _suggested(person["data_schema"], "person_entity_id") == "person.kid_a"
    dest = await _submit(hass, person, {"name": "Kiddo", "is_adult": False})
    assert dest["step_id"] == "profile_destination"
    assert _suggested(dest["data_schema"], "default_location") == "school"
    cals = await _submit(hass, dest, {"default_mode": "car", "weather_adjust": False})
    assert cals["step_id"] == "profile_calendars"
    margins = await _submit(hass, cals, {"calendars": []})
    notif = await _submit(
        hass,
        await _submit(hass, margins, _MARGINS),
        {"packing_match": "", "packing_item": ""},
    )
    test = await _submit(hass, notif, _NOTIFICATIONS)
    await _submit(hass, test)
    await hass.async_block_till_done()
    assert entry.options[OPT_PROFILES]["kid_a"]["name"] == "Kiddo"
    assert set(entry.options[OPT_PROFILES]) == {"kid_a"}

    # A second profile also named "Kid A" gets a numeric suffix.
    await _walk_profile(
        hass, entry, person="person.kid_a2", new_location=None, location="school"
    )
    assert set(entry.options[OPT_PROFILES]) == {"kid_a", "kid_a_2"}


_MARGINS = {
    "is_adult": False,
    "arrival": 0,
    "departure": 5,
    "boarding": 2,
    "parking_and_walk": 3,
    "min_transfer": 5,
    "weekday_mask": ["0", "1", "2", "3", "4"],
}
_NOTIFICATIONS = {
    "quiet_start": "22:00:00",
    "quiet_end": "06:00:00",
    "notifications_enabled": False,
    "evening_notice_enabled": True,
    "change_threshold_minutes": 3,
}


async def test_edit_profile_keeps_everything_it_was_not_asked_about(
    hass: HomeAssistant,
) -> None:
    """Clicking through an edit keeps the destination, links, copies and lunch."""
    main = _link("school", "school_cal", "main", exclude_patterns=["LUNCH"])
    extra = _link(
        "club",
        "club_cal",
        "extra",
        location_id="hall",
        rules=[{"match": "Simhall", "location_id": "hall", "mode": None}],
    )
    profile = _profile_v2(
        "kid_a",
        person="person.kid_a",
        links=(main, extra),
        copy_to=("kid_b",),
        location="hall",
    )
    profile["lunch_calendar_entity_id"] = "calendar.lunch"
    entry = await _setup_entry(
        hass,
        profiles={"kid_a": profile, "kid_b": _profile_v2("kid_b")},
        locations={"school": SCHOOL, "hall": HALL},
        calendars={"school_cal": SCHOOL_CAL, "club_cal": CLUB_CAL},
    )

    pick = await _open(hass, entry, "edit_profile")
    person = await _submit(hass, pick, {"profile_id": "kid_a"})
    dest = await _submit(hass, person, {"is_adult": False})
    assert _suggested(dest["data_schema"], "default_location") == "hall"
    cals = await _submit(hass, dest, {"default_mode": "car", "weather_adjust": False})
    assert _suggested(cals["data_schema"], "calendars") == ["school_cal", "club_cal"]
    assert _suggested(cals["data_schema"], "main_calendar") == "school_cal"
    link = await _submit(
        hass,
        cals,
        {"calendars": ["school_cal", "club_cal"], "main_calendar": "school_cal"},
    )
    assert _suggested(link["data_schema"], "exclude_patterns") == "LUNCH"
    link = await _submit(hass, link, {**LINK_FIELDS, "exclude_patterns": "LUNCH"})
    assert _suggested(link["data_schema"], "rules") == "Simhall => Sporthallen"
    assert _suggested(link["data_schema"], "location") == "hall"
    margins = await _submit(
        hass,
        link,
        {**LINK_FIELDS, "location": "hall", "rules": "Simhall => Sporthallen"},
    )
    packing = await _submit(hass, margins, _MARGINS)
    notif = await _submit(hass, packing, {"packing_match": "", "packing_item": ""})
    assert _suggested(notif["data_schema"], "copy_to") == ["kid_b"]
    assert _suggested(notif["data_schema"], "lunch_calendar_entity_id") == (
        "calendar.lunch"
    )
    test = await _submit(
        hass,
        notif,
        {
            **_NOTIFICATIONS,
            "copy_to": ["kid_b"],
            "lunch_in_digest": True,
            "copy_kinds": ["cleanup", "leave_now"],
            "lunch_calendar_entity_id": "calendar.lunch",
        },
    )
    await _submit(hass, test)
    await hass.async_block_till_done()

    saved = entry.options[OPT_PROFILES]["kid_a"]
    assert saved["default_location_id"] == "hall"
    assert saved["person_entity_id"] == "person.kid_a"
    assert saved["copy_to"] == ["kid_b"]
    assert saved["lunch_calendar_entity_id"] == "calendar.lunch"
    assert [link["calendar_id"] for link in saved["calendar_links"]] == [
        "school_cal",
        "club_cal",
    ]
    assert saved["calendar_links"][0]["exclude_patterns"] == ["LUNCH"]
    assert saved["calendar_links"][1]["rules"] == [
        {"match": "Simhall", "location_id": "hall", "mode": None}
    ]
    assert saved["calendar_links"][0]["link_id"] == "school"


async def test_add_then_remove_profile_cleans_entities_and_copies(
    hass: HomeAssistant,
) -> None:
    """Removing a profile purges its device and entities and others' copy_to."""
    entry = await _setup_entry(
        hass,
        profiles={"kid_b": _profile_v2("kid_b", copy_to=("kid_a", "kid_c"))},
        locations={"school": SCHOOL},
        calendars={"school_cal": SCHOOL_CAL},
    )
    await _walk_profile(
        hass, entry, new_location=None, location="school", calendars=["school_cal"]
    )
    profile_id = "kid_a"
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    device_identifier = (DOMAIN, f"{entry.entry_id}_{profile_id}")
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    assert [
        e for e in entities if e.unique_id.startswith(f"{entry.entry_id}_{profile_id}_")
    ], "expected the new profile to have registered entities"
    assert any(
        device_identifier in d.identifiers
        for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    )

    pick = await _open(hass, entry, "remove_profile")
    confirm = await _submit(hass, pick, {"profile_id": profile_id})
    assert confirm["step_id"] == "remove_confirm"
    done = await _submit(hass, confirm)
    await hass.async_block_till_done()

    assert done["type"] == FlowResultType.CREATE_ENTRY
    assert profile_id not in entry.options[OPT_PROFILES]
    # kid_b stops copying to the removed profile, but keeps its other recipient.
    assert entry.options[OPT_PROFILES]["kid_b"]["copy_to"] == ["kid_c"]
    # Only this profile used its calendar (the box defaults on); the location is
    # still kid_b's default, so it stays.
    assert entry.options["calendars"] == {}
    assert set(entry.options["locations"]) == {"school"}
    # Its device and every one of its entities are gone (cascade).
    assert not any(
        device_identifier in d.identifiers
        for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    )
    assert not [
        e
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if e.unique_id.startswith(f"{entry.entry_id}_{profile_id}_")
    ]


async def test_zero_profiles_entry_loads_no_missions(hass: HomeAssistant) -> None:
    """An entry with no profiles sets up cleanly and plans nothing (§4.1)."""
    entry = await _setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.options[OPT_PROFILES] == {}

    coordinator = entry.runtime_data.coordinator
    assert coordinator.data == {}


# ---------------------------------------------------------------------------
# Forms are frontend-renderable and location selectors have initial values
# ---------------------------------------------------------------------------


async def test_every_options_form_is_frontend_renderable(hass: HomeAssistant) -> None:
    """No field of any options form serializes to UNSUPPORTED (bare ``str``)."""
    entry = await _setup_entry(
        hass,
        locations={"school": SCHOOL},
        calendars={"school_cal": SCHOOL_CAL},
        profiles={"kid_b": _profile_v2("kid_b")},
    )
    forms: list[dict] = []

    result = await _open(hass, entry, "add_profile")
    forms.append(result)
    result = await _submit(
        hass,
        result,
        {"person_entity_id": "person.kid_a", "name": "Kid A", "is_adult": False},
    )
    forms.append(result)
    result = await _submit(
        hass,
        result,
        {"default_location": "school", "default_mode": "car", "weather_adjust": False},
    )
    forms.append(result)
    result = await _submit(hass, result, {"calendars": ["school_cal"]})
    forms.append(result)
    result = await _submit(hass, result, LINK_FIELDS)
    forms.append(result)
    result = await _submit(hass, result, _MARGINS)
    forms.append(result)
    result = await _submit(hass, result, {"packing_match": "", "packing_item": ""})
    forms.append(result)
    forms.append(await _open(hass, entry, "locations", "location_add"))
    forms.append(await _open(hass, entry, "calendars", "calendar_add"))
    forms.append(await _open(hass, entry, "settings"))

    steps = [form["step_id"] for form in forms]
    assert steps == [
        "profile_person",
        "profile_destination",
        "profile_calendars",
        "profile_link",
        "profile_margins",
        "profile_packing",
        "profile_notifications",
        "location_form",
        "calendar_form",
        "settings",
    ]
    for form in forms:
        _assert_schema_renderable(form["data_schema"])


async def test_location_selectors_have_initial_values_on_add(
    hass: HomeAssistant,
) -> None:
    """Every location selector must carry a non-null initial value (frontend crash)."""
    entry = await _setup_entry(hass)

    destination = await _submit(
        hass,
        await _open(hass, entry, "add_profile"),
        {"person_entity_id": "person.kid_a", "name": "Kid A", "is_adult": False},
    )
    assert destination["step_id"] == "profile_destination"
    _assert_location_fields_have_initial_value(destination["data_schema"])

    location_form = await _open(hass, entry, "locations", "location_add")
    _assert_location_fields_have_initial_value(location_form["data_schema"])


async def test_a_second_person_joining_a_calendar_protects_the_first(
    hass: HomeAssistant,
) -> None:
    """Kid A's existing extra link must stop matching events tagged for kid B."""
    entry = await _setup_entry(
        hass,
        profiles={
            "kid_a": _profile_v2("kid_a", person="person.kid_a"),
            "kid_b": _profile_v2("kid_b", person="person.kid_b"),
        },
        locations={"school": SCHOOL},
        calendars={"club_cal": CLUB_CAL},
    )
    club = {
        "name": "Klubben",
        "source_type": "ha_calendar",
        "calendar_entity_id": "calendar.club",
    }

    for persons in (["kid_a"], ["kid_a", "kid_b"]):
        pick = await _open(hass, entry, "calendars", "calendar_edit")
        form = await _submit(hass, pick, {"calendar_id": "club_cal"})
        await _submit(hass, form, {**club, "persons": persons})
        await hass.async_block_till_done()

    profiles = entry.options[OPT_PROFILES]
    for pid in ("kid_a", "kid_b"):
        [link] = profiles[pid]["calendar_links"]
        assert link["attendee_mode"] == "tagged_or_untagged", pid


async def test_a_profile_joining_a_shared_calendar_protects_the_others(
    hass: HomeAssistant,
) -> None:
    other = _link("club", "club_cal", "extra", attendee_mode="all")
    main = _link("school", "school_cal", "main", attendee_mode="all")
    entry = await _setup_entry(
        hass,
        profiles={"kid_b": _profile_v2("kid_b", links=(main, other))},
        locations={"school": SCHOOL},
        calendars={"club_cal": CLUB_CAL, "school_cal": SCHOOL_CAL},
    )
    await _walk_profile(
        hass,
        entry,
        new_location=None,
        location="school",
        calendars=["club_cal"],
    )

    kid_b = entry.options[OPT_PROFILES]["kid_b"]["calendar_links"]
    by_role = {link["role"]: link["attendee_mode"] for link in kid_b}
    # The newcomer's link defaults to tagged_or_untagged; kid_b's extra link
    # follows, while the main link (their school) is left alone.
    assert by_role == {"main": "all", "extra": "tagged_or_untagged"}
    [new_link] = entry.options[OPT_PROFILES]["kid_a"]["calendar_links"]
    assert new_link["attendee_mode"] == "tagged_or_untagged"


async def _remove_profile(
    hass: HomeAssistant, entry: MockConfigEntry, profile_id: str, **answer: Any
) -> dict:
    pick = await _open(hass, entry, "remove_profile")
    confirm = await _submit(hass, pick, {"profile_id": profile_id})
    assert confirm["step_id"] == "remove_confirm"
    done = await _submit(hass, confirm, answer)
    await hass.async_block_till_done()
    return confirm | {"done": done}


async def test_removing_a_profile_removes_its_private_calendar_url_and_location(
    hass: HomeAssistant,
) -> None:
    """The secret URL of a calendar only the removed profile used must not stay."""
    secret = "https://feeds.example.test/secret-token"
    ics_cal = {"id": "kid_a_calendar", "name": "Kid A schema", "source_type": "ics"}
    entry = await _setup_entry(
        hass,
        profiles={
            "kid_a": _profile_v2(
                "kid_a", links=(_link("main", "kid_a_calendar", "main"),)
            ),
            "kid_b": _profile_v2(
                "kid_b", links=(_link("main", "school_cal", "main"),), location="hall"
            ),
        },
        locations={"school": SCHOOL, "hall": HALL},
        calendars={"kid_a_calendar": ics_cal, "school_cal": SCHOOL_CAL},
        ics_urls={"kid_a_calendar": secret},
    )

    result = await _remove_profile(hass, entry, "kid_a")

    assert "Kid A schema" in result["description_placeholders"]["unused"]
    assert result["done"]["type"] == FlowResultType.CREATE_ENTRY
    assert set(entry.options["calendars"]) == {"school_cal"}
    assert set(entry.options["locations"]) == {"hall"}
    assert entry.data[DATA_ICS_URLS] == {}
    assert secret not in str(entry.data) + str(entry.options)


def _two_profiles_sharing_a_club() -> dict[str, Any]:
    shared = _link("club", "club_cal", "extra")
    return {
        "profiles": {
            "kid_a": _profile_v2(
                "kid_a", links=(shared, _link("main", "school_cal", "main"))
            ),
            "kid_b": _profile_v2("kid_b", links=(shared,)),
        },
        "locations": {"school": SCHOOL},
        "calendars": {"club_cal": CLUB_CAL, "school_cal": SCHOOL_CAL},
    }


async def test_remove_profile_keeps_a_calendar_another_profile_uses(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass, **_two_profiles_sharing_a_club())

    result = await _remove_profile(hass, entry, "kid_a")

    # Only the school calendar was kid_a's alone; kid_b still uses the club's,
    # and the school location is still kid_b's default.
    assert result["description_placeholders"]["unused"] == "Skolan"
    assert set(entry.options["calendars"]) == {"club_cal"}
    assert set(entry.options["locations"]) == {"school"}


async def test_remove_profile_can_leave_unused_resources_in_place(
    hass: HomeAssistant,
) -> None:
    entry = await _setup_entry(hass, **_two_profiles_sharing_a_club())

    await _remove_profile(hass, entry, "kid_a", remove_unused=False)

    assert set(entry.options[OPT_PROFILES]) == {"kid_b"}
    assert set(entry.options["calendars"]) == {"club_cal", "school_cal"}
