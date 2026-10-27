"""Config-entry v1 → v2 migration (plan §3).

These assert the product behaviour the migration promises:

* a v1 ICS profile becomes a calendar with the URL re-keyed by calendar id and
  no URL left under the profile id;
* two v1 HA-calendar profiles on the same entity collapse to one calendar with
  two links (F5 for free);
* two identical ICS URLs collapse to one calendar;
* a real v1 ``MockConfigEntry`` sets up end to end, ends at ``version == 2`` and
  keeps its entity unique ids (no entities recreated);
* a stored ``person:date:morning`` mission keeps its ledger through the upgrade,
  so no evening/morning notice is resent.

The migration itself is pure and never logs a URL value
(review-privacy-security).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch

from custom_components.family_departures import coordinator as coordinator_mod
from custom_components.family_departures.const import (
    DATA_HOME_LAT,
    DATA_HOME_LON,
    DATA_HOUSEHOLD_NAME,
    DATA_ICS_URLS,
    EVENT_NOTIFICATION,
    OPT_PROFILES,
)
from custom_components.family_departures.coordinator import ProviderFactories
from custom_components.family_departures.household import (
    migrate_v1_to_v2,
    parse_household,
)
from custom_components.family_departures.models import (
    CalendarSource,
    DurationResult,
    JourneyResult,
    ScheduleEvent,
    ScheduleResult,
)
from custom_components.family_departures.timeutil import combine_local
from freezegun import freeze_time
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

HOME = (59.33, 18.06)
SECRET_ICS_URL = "https://example.test/feed?token=SECRET"
# A Monday in the weekday mask.
NOW = datetime(2026, 10, 19, 5, 0, tzinfo=UTC)
TODAY = date(2026, 10, 19)


# ---------------------------------------------------------------------------
# v1 profile fixtures
# ---------------------------------------------------------------------------


def _v1_ics(profile_id: str) -> dict[str, Any]:
    return {
        "id": profile_id,
        "name": profile_id.title(),
        "source_type": "ics",
        "calendar_entity_id": None,
        "exclude_patterns": [],
        "include_patterns": [],
        "destination_id": f"{profile_id}_destination",
        "dest_lat": 59.4,
        "dest_lon": 18.1,
        "default_mode": "public_transport",
        "static_minutes": None,
        "static_label": None,
        "weather_adjust": False,
        "car_fallback_minutes": None,
        "arrival": 5,
        "departure": 5,
        "boarding": 2,
        "parking_and_walk": 0,
        "min_transfer": 5,
        "weekday_mask": ["0", "1", "2", "3", "4"],
        "packing_rules": [],
        "person_entity_id": None,
        "notifications_enabled": True,
        "evening_notice_enabled": True,
        "change_threshold_minutes": 3,
        "quiet_start": "22:00:00",
        "quiet_end": "06:00:00",
        "scripts": {"push": f"script.push_{profile_id}"},
    }


def _v1_ha_calendar(profile_id: str, entity_id: str) -> dict[str, Any]:
    profile = _v1_ics(profile_id)
    profile["source_type"] = "ha_calendar"
    profile["calendar_entity_id"] = entity_id
    return profile


# ---------------------------------------------------------------------------
# Pure-migration cases
# ---------------------------------------------------------------------------


def test_ics_profile_becomes_calendar_with_moved_url() -> None:
    data = {DATA_ICS_URLS: {"kid_a": SECRET_ICS_URL}}
    options = {OPT_PROFILES: {"kid_a": _v1_ics("kid_a")}}

    new_data, new_options = migrate_v1_to_v2(data, options)

    # The URL moved from the profile id to the calendar id; nothing under the
    # old profile key.
    calendar_id = "kid_a_calendar"
    assert new_data[DATA_ICS_URLS] == {calendar_id: SECRET_ICS_URL}
    assert "kid_a" not in new_data[DATA_ICS_URLS]

    # One calendar and one main link referencing it.
    calendars = new_options["calendars"]
    assert set(calendars) == {calendar_id}
    assert calendars[calendar_id]["source_type"] == "ics"
    profile = new_options[OPT_PROFILES]["kid_a"]
    links = profile["calendar_links"]
    assert len(links) == 1
    assert links[0]["calendar_id"] == calendar_id
    assert links[0]["role"] == "main"
    # The default location was created from the v1 destination coordinates.
    assert profile["default_location_id"] == "kid_a_destination"
    assert new_options["locations"]["kid_a_destination"]["lat"] == 59.4
    # The v1 source keys are gone.
    assert "source_type" not in profile
    assert "dest_lat" not in profile


def test_two_ha_calendar_profiles_on_one_entity_share_one_calendar() -> None:
    options = {
        OPT_PROFILES: {
            "kid_a": _v1_ha_calendar("kid_a", "calendar.familjen"),
            "kid_b": _v1_ha_calendar("kid_b", "calendar.familjen"),
        }
    }

    _new_data, new_options = migrate_v1_to_v2({DATA_ICS_URLS: {}}, options)

    calendars = new_options["calendars"]
    # Both profiles point at the same single calendar record (F5).
    assert len(calendars) == 1
    (calendar_id,) = calendars
    assert calendars[calendar_id]["calendar_entity_id"] == "calendar.familjen"
    links_a = new_options[OPT_PROFILES]["kid_a"]["calendar_links"]
    links_b = new_options[OPT_PROFILES]["kid_b"]["calendar_links"]
    assert links_a[0]["calendar_id"] == calendar_id
    assert links_b[0]["calendar_id"] == calendar_id


def test_two_identical_ics_urls_share_one_calendar() -> None:
    data = {DATA_ICS_URLS: {"kid_a": SECRET_ICS_URL, "kid_b": SECRET_ICS_URL}}
    options = {OPT_PROFILES: {"kid_a": _v1_ics("kid_a"), "kid_b": _v1_ics("kid_b")}}

    new_data, new_options = migrate_v1_to_v2(data, options)

    calendars = new_options["calendars"]
    assert len(calendars) == 1
    (calendar_id,) = calendars
    # The single URL is stored once, under the shared calendar id.
    assert new_data[DATA_ICS_URLS] == {calendar_id: SECRET_ICS_URL}
    links_a = new_options[OPT_PROFILES]["kid_a"]["calendar_links"]
    links_b = new_options[OPT_PROFILES]["kid_b"]["calendar_links"]
    assert links_a[0]["calendar_id"] == calendar_id
    assert links_b[0]["calendar_id"] == calendar_id


def test_parse_household_isolates_an_invalid_profile(caplog: Any) -> None:
    """One unparseable profile is skipped; its valid sibling still parses (§15).

    A bad profile only disables itself, matching v1's per-profile fault
    isolation. The whole household must not collapse because of one bad entry.
    """
    import logging

    # Migrate a valid v1 profile to get the v2 household shape, then add a
    # second profile that is a mapping but cannot build a ProfileConfig
    # (missing the required margin fields and default_mode).
    data = {DATA_ICS_URLS: {"kid_a": SECRET_ICS_URL}}
    _new_data, options = migrate_v1_to_v2(
        data, {OPT_PROFILES: {"kid_a": _v1_ics("kid_a")}}
    )
    options[OPT_PROFILES]["kid_b"] = {"id": "kid_b", "name": "Kid B"}

    with caplog.at_level(logging.DEBUG):
        household = parse_household(_new_data, options)

    # The valid profile parsed; the invalid one is simply absent.
    assert "kid_a" in household.profiles
    assert "kid_b" not in household.profiles
    # Nothing about the skipped profile leaked into the log here.
    assert "kid_b" not in caplog.text


def test_v1_profile_with_invalid_source_type_is_rejected_not_calendarless() -> None:
    """A profile the migration cannot reshape must not run as a profile with no
    calendar: it is rejected so the coordinator flags it invalid_config."""
    broken = _v1_ics("kid_b")
    broken["source_type"] = "carrier_pigeon"
    new_data, options = migrate_v1_to_v2(
        {DATA_ICS_URLS: {"kid_a": SECRET_ICS_URL}},
        {OPT_PROFILES: {"kid_a": _v1_ics("kid_a"), "kid_b": broken}},
    )

    household = parse_household(new_data, options)

    assert "kid_a" in household.profiles
    assert "kid_b" not in household.profiles


async def test_coordinator_flags_only_the_invalid_profile(
    hass: HomeAssistant,
) -> None:
    """End to end: a bad profile beside a valid one only disables itself.

    The coordinator builds a runtime for the valid profile and flags the bad
    one ``invalid_config``; planning is not disabled household-wide.
    """
    entry = _v1_entry({"kid_a": _v1_ics("kid_a")})
    # Inject a mapping-but-invalid v2 profile into the migrated options.
    new_data, new_options = migrate_v1_to_v2(entry.data, entry.options)
    new_options[OPT_PROFILES]["kid_b"] = {"id": "kid_b", "name": "Kid B"}
    entry = MockConfigEntry(
        domain="family_departures",
        unique_id="family_departures",
        title="Familjen",
        version=2,
        data=new_data,
        options=new_options,
    )
    entry.add_to_hass(hass)
    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}

    with freeze_time(NOW), _patch_factories(schedules):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    # The valid profile is still planned; only the bad one is flagged.
    assert "kid_a" in coordinator.data
    assert coordinator.invalid_sources.get("kid_b") == "invalid_config"
    assert "kid_a" not in coordinator.invalid_sources


def test_migration_does_not_log_url(caplog: Any) -> None:
    """The URL value never appears in logs (review-privacy-security, §10)."""
    import logging

    data = {DATA_ICS_URLS: {"kid_a": SECRET_ICS_URL}}
    options = {OPT_PROFILES: {"kid_a": _v1_ics("kid_a")}}
    with caplog.at_level(logging.DEBUG):
        migrate_v1_to_v2(data, options)
    assert SECRET_ICS_URL not in caplog.text
    assert "SECRET" not in caplog.text


# ---------------------------------------------------------------------------
# End-to-end setup through the integration's async_migrate_entry
# ---------------------------------------------------------------------------


class _FakeSchedule:
    def __init__(self, result: ScheduleResult) -> None:
        self.result = result

    async def async_get_day(self, d: date) -> ScheduleResult:
        return self.result


class _FakeJourney:
    def __init__(self, result: JourneyResult) -> None:
        self.result = result

    async def async_plan(self, *args: Any, **kwargs: Any) -> JourneyResult:
        return self.result


class _FakeCar:
    def __init__(self, result: DurationResult) -> None:
        self.result = result

    async def async_get_duration(self, *args: Any, **kwargs: Any) -> DurationResult:
        return self.result


def _ok_schedule() -> ScheduleResult:
    start = combine_local(TODAY, datetime.min.time().replace(hour=8, minute=20))
    return ScheduleResult(
        status="ok",
        events=(
            ScheduleEvent(
                uid="e1",
                summary="Lektion MA",
                start=start,
                end=start + timedelta(minutes=50),
                source_id="src",
            ),
        ),
        fetched_at=NOW,
    )


def _patch_factories(schedules: Mapping[str, _FakeSchedule]) -> Any:
    journey = _FakeJourney(JourneyResult(status="empty", journeys=(), fetched_at=NOW))
    car = _FakeCar(
        DurationResult(minutes=20.0, fetched_at=NOW, source="waze", quality="realtime")
    )

    def make_schedule(calendar: CalendarSource, ics_url: str | None) -> _FakeSchedule:
        return schedules[calendar.id.removesuffix("_calendar")]

    def _factory(hass: HomeAssistant) -> ProviderFactories:
        return ProviderFactories(
            schedule=make_schedule, journey=lambda: journey, car=lambda: car
        )

    return patch.object(coordinator_mod, "_default_factories", _factory)


def _v1_entry(profiles: Mapping[str, dict[str, Any]]) -> MockConfigEntry:
    """A version-1 entry, as a pre-upgrade install would have on disk."""
    return MockConfigEntry(
        domain="family_departures",
        unique_id="family_departures",
        title="Familjen",
        version=1,
        data={
            DATA_HOUSEHOLD_NAME: "Familjen",
            DATA_HOME_LAT: HOME[0],
            DATA_HOME_LON: HOME[1],
            DATA_ICS_URLS: {pid: SECRET_ICS_URL for pid in profiles},
        },
        options={OPT_PROFILES: dict(profiles)},
    )


async def test_v1_entry_sets_up_and_reaches_version_2(hass: HomeAssistant) -> None:
    """A v1 entry migrates on setup, ends at version 2 and keeps its entity ids."""
    entry = _v1_entry({"kid_a": _v1_ics("kid_a")})
    entry.add_to_hass(hass)
    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}

    with freeze_time(NOW), _patch_factories(schedules):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.version == 2
    # The migrated options carry the v2 household shape.
    assert "calendars" in entry.options
    assert "kid_a" not in entry.data[DATA_ICS_URLS]
    assert entry.data[DATA_ICS_URLS] == {"kid_a_calendar": SECRET_ICS_URL}

    ent_reg = er.async_get(hass)
    unique_ids = {
        e.unique_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    }
    # The per-profile unique ids are unchanged (keyed on profile id, not source).
    assert f"{entry.entry_id}_kid_a_recommended_leave_time" in unique_ids


async def test_stored_morning_mission_keeps_ledger_no_resend(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """A v1.1 store's morning ledger survives the upgrade; no evening/morning resend.

    The store minor-2 migration keeps missions and their ledger; the config
    migration keeps the mission id ``person:date:morning``. So the evening and
    morning notices already recorded are not sent again on the first round.
    """
    entry = _v1_entry({"kid_a": _v1_ics("kid_a")})
    entry.add_to_hass(hass)
    mission_id = f"kid_a:{TODAY.isoformat()}:morning"
    # Seed the on-disk store (v1.1) with a mission whose evening + morning
    # notices were already sent.
    hass_storage[f"family_departures.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"family_departures.{entry.entry_id}",
        "data": {
            "overrides": {},
            "missions": {
                mission_id: {
                    "mission_id": mission_id,
                    "status": "scheduled",
                    "departed_at": None,
                    "reopened": False,
                    "notified": {
                        "evening": {
                            "kind": "evening",
                            "sent_at": (NOW - timedelta(hours=11)).isoformat(),
                            "leave_time": None,
                            "revision": 1,
                        },
                        "morning": {
                            "kind": "morning",
                            "sent_at": NOW.isoformat(),
                            "leave_time": None,
                            "revision": 1,
                        },
                    },
                    "first_published_leave": None,
                    "action_nonce": "seednonce",
                }
            },
            "packing_acks": {},
            "schedules": {
                "kid_a_destination|" + TODAY.isoformat(): {
                    "status": "ok",
                    "events": [],
                    "fetched_at": NOW.isoformat(),
                    "source_modified_at": None,
                    "content_hash": None,
                    "error_code": None,
                    "stale": False,
                }
            },
            "plans": {},
        },
    }

    events: list[Event] = []
    hass.bus.async_listen(EVENT_NOTIFICATION, lambda e: events.append(e))

    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}
    # Run at the −60 morning threshold so a *fresh* mission would emit a morning
    # notice; the preserved ledger must suppress it.
    morning_time = combine_local(TODAY, datetime.min.time().replace(hour=7, minute=20))
    with freeze_time(morning_time), _patch_factories(schedules):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # The ledger survived: no evening or morning notice was re-sent.
    kinds = {e.data.get("kind") for e in events}
    assert "evening" not in kinds
    assert "morning" not in kinds

    # The mission kept its ledger (both kinds still recorded).
    store = entry.runtime_data.coordinator._store
    mission = store.get_mission(mission_id)
    assert mission is not None
    assert "evening" in mission.notified
    assert "morning" in mission.notified
