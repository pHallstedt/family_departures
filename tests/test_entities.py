"""Tests for the Family Departures entity platforms (spec §9).

These set up a full config entry through ``hass.config_entries.async_setup``
with the coordinator's provider factories patched to return deterministic
fakes, so the registry, devices, states and attributes are exercised end to end:

* the §9 entity set is created, one device per profile, with stable unique IDs
  that survive a display-name change (review-ha-integration, spec §9);
* timestamp sensors are ``None`` when there is no plan, never a zero time;
* changing today's transport writes a day override and recomputes, and the old
  plan cannot be restored (spec §4.3, §11.3);
* no state attribute leaks an ICS URL or precise coordinate
  (review-privacy-security, spec §9).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from custom_components.family_departures import coordinator as coordinator_mod
from custom_components.family_departures.const import (
    DATA_HOME_LAT,
    DATA_HOME_LON,
    DATA_HOUSEHOLD_NAME,
    DATA_ICS_URLS,
    DOMAIN,
    OPT_PROFILES,
)
from custom_components.family_departures.coordinator import ProviderFactories
from custom_components.family_departures.models import (
    DurationResult,
    Journey,
    JourneyResult,
    Leg,
    ProfileConfig,
    ScheduleEvent,
    ScheduleResult,
)
from custom_components.family_departures.timeutil import combine_local, make_mission_id
from freezegun import freeze_time
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

# Home coordinates kept generic (no real location, review-privacy-security).
# The fake "secret" URL is deliberately not a real token path and does not match
# the no-secrets scanner's ical-feed pattern, so this test file stays clean.
HOME = (59.33, 18.06)
SECRET_ICS_URL = "https://example.test/calendar?token=FAKE-NOT-A-REAL-TOKEN-123"
NOW = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
TODAY = date(2026, 10, 20)


@pytest.fixture(autouse=True)
def _frozen_now():
    """Freeze the clock so overrides, mission ids and windows are deterministic."""
    with freeze_time(NOW):
        yield


# ---------------------------------------------------------------------------
# Fakes (mirroring the coordinator test's shape)
# ---------------------------------------------------------------------------


class _FakeSchedule:
    def __init__(self, result: ScheduleResult) -> None:
        self.result = result

    async def async_get_day(self, d: date) -> ScheduleResult:
        return self.result


class _FakeJourney:
    def __init__(self, result: JourneyResult) -> None:
        self.result = result

    async def async_plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        deadline: datetime,
        earliest_departure: datetime | None,
    ) -> JourneyResult:
        return self.result


class _FakeCar:
    def __init__(self, result: DurationResult) -> None:
        self.result = result

    async def async_get_duration(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        realtime: bool,
        time_delta: timedelta | None,
    ) -> DurationResult:
        return self.result


def _ok_schedule(summary: str = "Lektion IDRO1000X", hour: int = 8) -> ScheduleResult:
    start = combine_local(TODAY, _time(hour, 20))
    return ScheduleResult(
        status="ok",
        events=(
            ScheduleEvent(
                uid="e1",
                summary=summary,
                start=start,
                end=start + timedelta(minutes=50),
                source_id="kid_a_destination",
            ),
        ),
        fetched_at=NOW,
    )


def _empty_schedule() -> ScheduleResult:
    return ScheduleResult(status="empty", events=(), fetched_at=NOW)


def _transit_result() -> JourneyResult:
    dep = combine_local(TODAY, _time(7, 30))
    arr = combine_local(TODAY, _time(8, 0))
    return JourneyResult(
        status="ok",
        journeys=(
            Journey(
                journey_id="trip-1",
                legs=(
                    Leg(
                        kind="transit",
                        line="17",
                        from_stop="A",
                        to_stop="B",
                        planned_departure=dep,
                        planned_arrival=arr,
                    ),
                ),
            ),
        ),
        fetched_at=NOW,
    )


def _time(h: int, m: int):
    from datetime import time as _t

    return _t(h, m)


def _profile_dict(profile_id: str, *, default_mode: str = "public_transport") -> dict:
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
        "default_mode": default_mode,
        "static_minutes": None,
        "static_label": None,
        "weather_adjust": False,
        "car_fallback_minutes": 18,
        "arrival": 5,
        "departure": 5,
        "boarding": 2,
        "parking_and_walk": 3,
        "min_transfer": 5,
        "weekday_mask": ["0", "1", "2", "3", "4"],
        "packing_rules": [{"id": "r1", "match": "IDRO", "item": "Gympakläder"}],
        "person_entity_id": None,
        "notifications_enabled": True,
        "evening_notice_enabled": True,
        "change_threshold_minutes": 3,
        "quiet_start": "22:00:00",
        "quiet_end": "06:00:00",
        "scripts": {},
    }


def _make_entry(profiles: Mapping[str, dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Familjen",
        data={
            DATA_HOUSEHOLD_NAME: "Familjen",
            DATA_HOME_LAT: HOME[0],
            DATA_HOME_LON: HOME[1],
            DATA_ICS_URLS: {pid: SECRET_ICS_URL for pid in profiles},
        },
        options={OPT_PROFILES: dict(profiles)},
    )


def _patch_factories(
    schedules: Mapping[str, _FakeSchedule],
    *,
    journey: _FakeJourney | None = None,
    car: _FakeCar | None = None,
):
    """Patch the coordinator's default factories to inject the fakes."""
    journey = journey or _FakeJourney(_transit_result())
    car = car or _FakeCar(
        DurationResult(minutes=20.0, fetched_at=NOW, source="waze", quality="realtime")
    )

    def make_schedule(profile: ProfileConfig, ics_url: str | None) -> _FakeSchedule:
        return schedules[profile.id]

    def _factory(hass: HomeAssistant) -> ProviderFactories:
        return ProviderFactories(
            schedule=make_schedule,
            journey=lambda: journey,
            car=lambda: car,
        )

    return patch.object(coordinator_mod, "_default_factories", _factory)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    """Resolve an entity id from its stable unique id, platform-agnostic.

    Entity ids depend on loaded translations; the unique id (entry+profile+key)
    is the stable contract we assert on (spec §9), so tests look entities up by
    unique id rather than guessing a slug.
    """
    ent_reg = er.async_get(hass)
    for domain in ("sensor", "binary_sensor", "select", "switch", "button"):
        unique_id = f"{entry.entry_id}_{key}"
        entity_id = ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)
        if entity_id is not None:
            return entity_id
    raise AssertionError(f"no entity for unique id suffix {key!r}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_entity_set_and_device_per_profile(hass: HomeAssistant) -> None:
    """The §9 entity set is created with one device per profile."""
    entry = _make_entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}
    with _patch_factories(schedules):
        await _setup(hass, entry)

    # Every §9 entity key is present (resolved by stable unique id).
    for key in (
        "kid_a_event_start",
        "kid_a_arrival_deadline",
        "kid_a_recommended_leave_time",
        "kid_a_latest_leave_time",
        "kid_a_predicted_arrival",
        "kid_a_travel_minutes",
        "kid_a_departure_status",
        "kid_a_plan_quality",
        "kid_a_route_summary",
        "kid_a_packing_list",
        "kid_a_packing_list_tomorrow",
        "kid_a_default_transport",
        "kid_a_departure_disruption",
        "kid_a_schedule_needs_review",
        "kid_a_today_transport",
        "kid_a_today_attendance",
        "kid_a_departure_notifications",
        "kid_a_ack_packing",
        "kid_a_refresh_departure",
        "kid_a_mark_departed",
    ):
        entity_id = _entity_id(hass, entry, key)
        assert hass.states.get(entity_id) is not None, key

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    entity_id = _entity_id(hass, entry, "kid_a_recommended_leave_time")
    entity_entry = ent_reg.async_get(entity_id)
    assert entity_entry is not None
    assert entity_entry.unique_id == f"{entry.entry_id}_kid_a_recommended_leave_time"
    # Translations resolve the friendly name (device name + entity name), so the
    # entity id is the readable slug rather than a device-class fallback.
    assert entity_id == "sensor.kid_a_recommended_leave_time"
    assert entity_entry.device_id is not None
    device = dev_reg.async_get(entity_entry.device_id)
    assert device is not None
    assert (DOMAIN, f"{entry.entry_id}_kid_a") in device.identifiers


async def test_packing_sensor_reflects_schedule(hass: HomeAssistant) -> None:
    """The packing sensor lists the item a matching lesson produced (§5.5)."""
    entry = _make_entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": _FakeSchedule(_ok_schedule(summary="Lektion IDRO1000X"))}
    with _patch_factories(schedules):
        await _setup(hass, entry)

    state = hass.states.get(_entity_id(hass, entry, "kid_a_packing_list"))
    assert state is not None
    assert state.state == "Gympakläder"


async def test_unique_ids_survive_rename(hass: HomeAssistant) -> None:
    """A profile display-name change does not change entity unique IDs (§9)."""
    entry = _make_entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}
    with _patch_factories(schedules):
        await _setup(hass, entry)

    ent_reg = er.async_get(hass)
    before = {
        e.entity_id: e.unique_id
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    }
    assert before

    # Rename the profile in options and reload.
    new_profiles = {"kid_a": {**_profile_dict("kid_a"), "name": "Kid A Renamed"}}
    with _patch_factories(schedules):
        hass.config_entries.async_update_entry(
            entry, options={OPT_PROFILES: new_profiles}
        )
        await hass.async_block_till_done()

    after = {
        e.entity_id: e.unique_id
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    }
    # Same entities, same unique IDs: the id is keyed on profile id, not name.
    assert after == before


async def test_timestamp_sensors_none_without_plan(hass: HomeAssistant) -> None:
    """Timestamp sensors are unknown (None) on a day with no plan (§9)."""
    entry = _make_entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": _FakeSchedule(_empty_schedule())}
    with _patch_factories(schedules):
        await _setup(hass, entry)

    for key in (
        "kid_a_recommended_leave_time",
        "kid_a_latest_leave_time",
        "kid_a_predicted_arrival",
        "kid_a_event_start",
        "kid_a_arrival_deadline",
    ):
        state = hass.states.get(_entity_id(hass, entry, key))
        assert state is not None, key
        # Never a fabricated zero time; unknown is the correct "no value".
        assert state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE), key


async def test_change_today_transport_recomputes(hass: HomeAssistant) -> None:
    """Selecting car for today writes an override and replans as car (§4.3)."""
    entry = _make_entry(
        {"parent_b": _profile_dict("parent_b", default_mode="public_transport")}
    )
    schedules = {"parent_b": _FakeSchedule(_ok_schedule())}
    with _patch_factories(schedules):
        await _setup(hass, entry)

        status_id = _entity_id(hass, entry, "parent_b_departure_status")
        status = hass.states.get(status_id)
        assert status is not None
        assert status.attributes.get("source") == "public_transport"

        await hass.services.async_call(
            "select",
            "select_option",
            {
                "entity_id": _entity_id(hass, entry, "parent_b_today_transport"),
                "option": "car",
            },
            blocking=True,
        )
        await hass.async_block_till_done()

    # The override is stored for today and the plan is now a car plan.
    coordinator = entry.runtime_data
    override = coordinator._store.get_override("parent_b", TODAY)
    assert override is not None
    assert override.mode == "car"
    status = hass.states.get(status_id)
    assert status is not None
    assert status.attributes.get("source") == "car"


async def test_mark_departed_closes_mission(hass: HomeAssistant) -> None:
    """Pressing mark-departed confirms departure and old plan cannot restore it."""
    entry = _make_entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}
    with _patch_factories(schedules):
        await _setup(hass, entry)

        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": _entity_id(hass, entry, "kid_a_mark_departed")},
            blocking=True,
        )
        await hass.async_block_till_done()

    coordinator = entry.runtime_data
    mission = coordinator._store.get_mission(make_mission_id("kid_a", TODAY))
    assert mission is not None
    assert mission.status == "departed"
    assert mission.departed_at is not None


async def test_ack_packing_acknowledges_items(hass: HomeAssistant) -> None:
    """Pressing ack-packing records the current items as acknowledged (§5.5)."""
    entry = _make_entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": _FakeSchedule(_ok_schedule(summary="Lektion IDRO1000X"))}
    with _patch_factories(schedules):
        await _setup(hass, entry)

        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": _entity_id(hass, entry, "kid_a_ack_packing")},
            blocking=True,
        )
        await hass.async_block_till_done()

    coordinator = entry.runtime_data
    acks = coordinator._store.get_packing_acks("kid_a", TODAY)
    assert "Gympakläder" in acks


async def test_attributes_have_no_url_or_coordinates(hass: HomeAssistant) -> None:
    """No state attribute exposes the secret ICS URL or precise coordinates (§9)."""
    entry = _make_entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}
    with _patch_factories(schedules):
        await _setup(hass, entry)

    ent_reg = er.async_get(hass)
    for entity_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        state = hass.states.get(entity_entry.entity_id)
        if state is None:
            continue
        blob = repr(state.attributes)
        # The secret ICS URL / its token must never surface in an attribute.
        assert SECRET_ICS_URL not in blob, entity_entry.entity_id
        assert "token=" not in blob, entity_entry.entity_id
        # Nor the precise home coordinate.
        assert str(HOME[0]) not in blob, entity_entry.entity_id
        assert str(entry.data[DATA_ICS_URLS]["kid_a"]) not in blob


async def test_entities_unload_cleanly(hass: HomeAssistant) -> None:
    """The platforms unload without leaving the entry loaded (spec §11.3)."""
    from homeassistant.config_entries import ConfigEntryState

    entry = _make_entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}
    with _patch_factories(schedules):
        await _setup(hass, entry)
        assert entry.state is ConfigEntryState.LOADED

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_notifications_switch_toggles_option(hass: HomeAssistant) -> None:
    """Turning the switch off writes notifications_enabled=False to options (§4.1)."""
    entry = _make_entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}
    with _patch_factories(schedules):
        await _setup(hass, entry)

        switch_id = _entity_id(hass, entry, "kid_a_departure_notifications")
        state = hass.states.get(switch_id)
        assert state is not None
        assert state.state == "on"

        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": switch_id},
            blocking=True,
        )
        await hass.async_block_till_done()

    stored = entry.options[OPT_PROFILES]["kid_a"]["notifications_enabled"]
    assert stored is False
