"""Tests for the Family Departures services (spec §14).

Each service is exercised through ``hass.services.async_call`` against a full
config entry whose provider factories are patched to deterministic fakes, so
the registration, voluptuous schemas, validation and Store side effects are
checked end to end:

* every service's happy path takes effect (override written, mission closed,
  journey recorded, alternatives returned);
* an invalid ``person_id``/``plan_id`` raises ``ServiceValidationError`` and an
  unparsable date is rejected by the schema;
* ``set_override``'s ``arrival_time`` is combined with the given *local* date in
  ``Europe/Stockholm`` on a DST date, not the server's UTC date (spec §14);
* ``get_alternatives`` returns response data (``SupportsResponse.ONLY``);
* the services are removed when the last entry unloads (spec §15).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch

import pytest
import voluptuous as vol
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
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

HOME = (59.33, 18.06)
SECRET_ICS_URL = "https://example.test/calendar?token=FAKE-NOT-A-REAL-TOKEN-123"
NOW = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
TODAY = date(2026, 10, 20)


@pytest.fixture(autouse=True)
def _frozen_now():
    with freeze_time(NOW):
        yield


# ---------------------------------------------------------------------------
# Fakes (shared shape with the coordinator/entity tests)
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


def _ok_schedule(hour: int = 8) -> ScheduleResult:
    start = combine_local(TODAY, time(hour, 20))
    return ScheduleResult(
        status="ok",
        events=(
            ScheduleEvent(
                uid="e1",
                summary="Lektion IDRO1000X",
                start=start,
                end=start + timedelta(minutes=50),
                source_id="kid_a_destination",
            ),
        ),
        fetched_at=NOW,
    )


def _transit_result() -> JourneyResult:
    dep = combine_local(TODAY, time(7, 30))
    arr = combine_local(TODAY, time(8, 0))
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


async def _setup_transit(hass: HomeAssistant) -> MockConfigEntry:
    entry = _make_entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}
    with _patch_factories(schedules):
        await _setup(hass, entry)
    return entry


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_services_registered_on_setup(hass: HomeAssistant) -> None:
    """All seven §14 services exist once the entry is set up."""
    await _setup_transit(hass)
    for service in (
        "refresh",
        "mark_departed",
        "set_override",
        "clear_override",
        "get_alternatives",
        "select_journey",
        "reopen_today",
    ):
        assert hass.services.has_service(DOMAIN, service), service


async def test_services_removed_on_last_unload(hass: HomeAssistant) -> None:
    """Unloading the last entry removes the shared services (spec §15)."""
    entry = await _setup_transit(hass)
    assert hass.services.has_service(DOMAIN, "refresh")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    for service in ("refresh", "mark_departed", "get_alternatives"):
        assert not hass.services.has_service(DOMAIN, service), service


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


async def test_refresh_all_and_single(hass: HomeAssistant) -> None:
    """refresh works with and without a person_id (spec §14)."""
    entry = await _setup_transit(hass)
    coordinator = entry.runtime_data

    with patch.object(
        coordinator, "async_request_refresh", wraps=coordinator.async_request_refresh
    ) as refresh:
        await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
        await hass.services.async_call(
            DOMAIN, "refresh", {"person_id": "kid_a"}, blocking=True
        )
    assert refresh.call_count == 2


async def test_refresh_unknown_person_raises(hass: HomeAssistant) -> None:
    """An unknown person_id is a validation error, not a silent no-op."""
    await _setup_transit(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "refresh", {"person_id": "ghost"}, blocking=True
        )


# ---------------------------------------------------------------------------
# mark_departed
# ---------------------------------------------------------------------------


async def test_mark_departed_closes_mission(hass: HomeAssistant) -> None:
    """mark_departed confirms the departure for the current plan (spec §10)."""
    entry = await _setup_transit(hass)
    coordinator = entry.runtime_data
    plan = coordinator.data["kid_a"].plan
    assert plan is not None

    await hass.services.async_call(
        DOMAIN,
        "mark_departed",
        {"person_id": "kid_a", "plan_id": plan.plan_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    mission = coordinator._store.get_mission(make_mission_id("kid_a", TODAY))
    assert mission is not None
    assert mission.status == "departed"
    assert mission.departed_at is not None


async def test_mark_departed_wrong_plan_raises(hass: HomeAssistant) -> None:
    """A stale/unknown plan_id is rejected (spec §14 validation)."""
    await _setup_transit(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "mark_departed",
            {"person_id": "kid_a", "plan_id": "not-the-plan"},
            blocking=True,
        )


# ---------------------------------------------------------------------------
# set_override / clear_override
# ---------------------------------------------------------------------------


async def test_set_override_writes_future_day(hass: HomeAssistant) -> None:
    """set_override stores a mode override for a future date (spec §14)."""
    entry = await _setup_transit(hass)
    coordinator = entry.runtime_data
    future = TODAY + timedelta(days=3)

    await hass.services.async_call(
        DOMAIN,
        "set_override",
        {"person_id": "kid_a", "date": future.isoformat(), "mode": "car"},
        blocking=True,
    )
    await hass.async_block_till_done()

    override = coordinator._store.get_override("kid_a", future)
    assert override is not None
    assert override.mode == "car"


async def test_set_override_arrival_time_is_local(hass: HomeAssistant) -> None:
    """arrival_time is kept as a wall-clock time combined with the local date.

    On a date inside summer time, combining the stored time with the local date
    must land on the Stockholm wall clock (CEST = UTC+2), not the server's UTC
    date (spec §14).
    """
    entry = await _setup_transit(hass)
    coordinator = entry.runtime_data
    summer_day = date(2026, 7, 1)  # CEST, UTC+2

    await hass.services.async_call(
        DOMAIN,
        "set_override",
        {
            "person_id": "kid_a",
            "date": summer_day.isoformat(),
            "arrival_time": "08:30:00",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    override = coordinator._store.get_override("kid_a", summer_day)
    assert override is not None
    assert override.arrival_time == time(8, 30)
    # Combined with the local date, 08:30 local is 06:30 UTC in summer.
    combined = combine_local(override.local_date, override.arrival_time)
    assert combined == datetime(2026, 7, 1, 6, 30, tzinfo=UTC)


async def test_clear_override_removes_the_day(hass: HomeAssistant) -> None:
    """clear_override drops a stored override so the day uses defaults."""
    entry = await _setup_transit(hass)
    coordinator = entry.runtime_data
    future = TODAY + timedelta(days=2)

    await hass.services.async_call(
        DOMAIN,
        "set_override",
        {"person_id": "kid_a", "date": future.isoformat(), "mode": "static"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert coordinator._store.get_override("kid_a", future) is not None

    await hass.services.async_call(
        DOMAIN,
        "clear_override",
        {"person_id": "kid_a", "date": future.isoformat()},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert coordinator._store.get_override("kid_a", future) is None


async def test_set_override_invalid_date_rejected(hass: HomeAssistant) -> None:
    """A non-date value is rejected by the schema (spec §14 validation)."""
    await _setup_transit(hass)
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "set_override",
            {"person_id": "kid_a", "date": "not-a-date", "mode": "car"},
            blocking=True,
        )


# ---------------------------------------------------------------------------
# get_alternatives
# ---------------------------------------------------------------------------


async def test_get_alternatives_returns_response(hass: HomeAssistant) -> None:
    """get_alternatives returns the reachable journeys as response data."""
    await _setup_transit(hass)
    response = await hass.services.async_call(
        DOMAIN,
        "get_alternatives",
        {"person_id": "kid_a"},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    assert response["person_id"] == "kid_a"
    alternatives = response["alternatives"]
    assert alternatives
    assert alternatives[0]["selected"] is True
    assert alternatives[0]["journey_id"] == "trip-1"


async def test_get_alternatives_unknown_person_raises(hass: HomeAssistant) -> None:
    """get_alternatives validates the person id (spec §14)."""
    await _setup_transit(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "get_alternatives",
            {"person_id": "ghost"},
            blocking=True,
            return_response=True,
        )


# ---------------------------------------------------------------------------
# select_journey
# ---------------------------------------------------------------------------


async def test_select_journey_records_choice(hass: HomeAssistant) -> None:
    """select_journey records the chosen journey on the stored plan (spec §14)."""
    entry = await _setup_transit(hass)
    coordinator = entry.runtime_data
    plan = coordinator.data["kid_a"].plan
    assert plan is not None

    await hass.services.async_call(
        DOMAIN,
        "select_journey",
        {
            "person_id": "kid_a",
            "plan_id": plan.plan_id,
            "journey_id": "trip-chosen",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    stored = coordinator._store.get_plan(plan.mission_id)
    assert stored is not None
    assert stored.journey_id == "trip-chosen"


async def test_select_journey_wrong_plan_raises(hass: HomeAssistant) -> None:
    """select_journey rejects a stale plan id (spec §14 validation)."""
    await _setup_transit(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "select_journey",
            {
                "person_id": "kid_a",
                "plan_id": "stale-plan",
                "journey_id": "trip-1",
            },
            blocking=True,
        )


# ---------------------------------------------------------------------------
# reopen_today
# ---------------------------------------------------------------------------


async def test_reopen_today_unlocks_departed_mission(hass: HomeAssistant) -> None:
    """reopen_today sets the reopened flag so changes take effect again (§5.3)."""
    entry = await _setup_transit(hass)
    coordinator = entry.runtime_data
    plan = coordinator.data["kid_a"].plan
    assert plan is not None

    # Close the mission first.
    await hass.services.async_call(
        DOMAIN,
        "mark_departed",
        {"person_id": "kid_a", "plan_id": plan.plan_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN, "reopen_today", {"person_id": "kid_a"}, blocking=True
    )
    await hass.async_block_till_done()

    mission = coordinator._store.get_mission(make_mission_id("kid_a", TODAY))
    assert mission is not None
    assert mission.reopened is True
