"""Tests for the Family Departures coordinator (spec §3, §11.1, §11.2).

These exercise the coordinator against *fake* providers injected through
:class:`ProviderFactories`, with a real :class:`FamilyDeparturesStore` backed by
the PHACC ``hass_storage`` fixture. The focus is coordinator behaviour, not the
pure planner/schedule logic (covered elsewhere):

* one profile's source error does not fail the others (spec §15);
* a ``static`` plan makes no network calls (spec §6.4, §11.1);
* the tomorrow preview does not replace today's published plan (spec §11.1);
* a config change during a round discards that round's stale results (spec
  §11.2);
* the derived update interval tightens through the morning window (spec §11.1);
* a confirmed departure is never reset by a later recompute (spec §11.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from custom_components.family_departures.const import (
    DATA_HOME_LAT,
    DATA_HOME_LON,
    DATA_HOUSEHOLD_NAME,
    DATA_ICS_URLS,
    DOMAIN,
    OPT_PROFILES,
)
from custom_components.family_departures.coordinator import (
    DEFAULT_UPDATE_INTERVAL,
    IDLE_UPDATE_INTERVAL,
    IMMINENT_UPDATE_INTERVAL,
    NEAR_UPDATE_INTERVAL,
    FamilyDeparturesCoordinator,
    ProviderFactories,
)
from custom_components.family_departures.models import (
    DurationResult,
    Journey,
    JourneyResult,
    Leg,
    MissionState,
    ProfileConfig,
    ScheduleEvent,
    ScheduleResult,
)
from custom_components.family_departures.store import FamilyDeparturesStore
from custom_components.family_departures.timeutil import combine_local, make_mission_id
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

HOME = (59.33, 18.06)
# A fixed morning "now"; local date 2026-10-20 (a Tuesday, inside the mask).
NOW = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
TODAY = date(2026, 10, 20)


@pytest.fixture(autouse=True)
def _frozen_now():
    """Freeze ``dt_util.utcnow`` so the coordinator's clock equals ``NOW``.

    The pure planner/schedule take ``now`` explicitly, but the coordinator reads
    the real clock; freezing it keeps the stored-override dates, mission ids and
    the §11.1 window arithmetic deterministic.
    """
    with freeze_time(NOW):
        yield


# ---------------------------------------------------------------------------
# Stored profile fixtures (same shape the options flow writes, T12)
# ---------------------------------------------------------------------------


def _profile_dict(
    profile_id: str,
    *,
    default_mode: str = "public_transport",
    static_minutes: int | None = None,
    car_fallback_minutes: int | None = None,
) -> dict[str, Any]:
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
        "static_minutes": static_minutes,
        "static_label": "Cykel" if default_mode == "static" else None,
        "weather_adjust": False,
        "car_fallback_minutes": car_fallback_minutes,
        "arrival": 5,
        "departure": 5,
        "boarding": 2,
        "parking_and_walk": 3,
        "min_transfer": 5,
        "weekday_mask": ["0", "1", "2", "3", "4"],
        "packing_rules": [{"id": "r1", "match": "IDRO", "item": "Gympakläder"}],
        "person_entity_id": None,
        "notifications_enabled": False,
        "evening_notice_enabled": True,
        "change_threshold_minutes": 3,
        "quiet_start": "22:00:00",
        "quiet_end": "06:00:00",
        "scripts": {},
    }


def _entry(profiles: Mapping[str, dict[str, Any]]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Familjen",
        data={
            DATA_HOUSEHOLD_NAME: "Familjen",
            DATA_HOME_LAT: HOME[0],
            DATA_HOME_LON: HOME[1],
            # Every profile is ICS; give each a (fake) URL so the provider
            # factory accepts it. The fake schedule provider ignores the URL.
            DATA_ICS_URLS: {pid: f"https://example.test/{pid}" for pid in profiles},
        },
        options={OPT_PROFILES: dict(profiles)},
    )


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


class FakeSchedule:
    """A schedule provider that returns a pre-set result and counts calls."""

    def __init__(self, result: ScheduleResult) -> None:
        self.result = result
        self.calls: list[date] = []

    async def async_get_day(self, d: date) -> ScheduleResult:
        self.calls.append(d)
        return self.result


class FakeJourney:
    """A journey provider returning a fixed result and counting calls."""

    def __init__(self, result: JourneyResult) -> None:
        self.result = result
        self.calls = 0

    async def async_plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        deadline: datetime,
        earliest_departure: datetime | None,
    ) -> JourneyResult:
        self.calls += 1
        return self.result


class FakeCar:
    """A car provider returning a fixed duration and counting calls."""

    def __init__(self, result: DurationResult) -> None:
        self.result = result
        self.calls = 0

    async def async_get_duration(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        realtime: bool,
        time_delta: timedelta | None,
    ) -> DurationResult:
        self.calls += 1
        return self.result


def _ok_schedule(summary: str = "Lektion MA", hour: int = 8) -> ScheduleResult:
    start = combine_local(TODAY, _time(hour, 20))
    return ScheduleResult(
        status="ok",
        events=(
            ScheduleEvent(
                uid="e1",
                summary=summary,
                start=start,
                end=start + timedelta(minutes=50),
                source_id="src",
            ),
        ),
        fetched_at=NOW,
    )


def _error_schedule() -> ScheduleResult:
    return ScheduleResult(
        status="error",
        events=(),
        fetched_at=NOW,
        error_code="http_error",
        stale=True,
    )


def _transit_result() -> JourneyResult:
    dep = combine_local(TODAY, _time(7, 30))
    arr = combine_local(TODAY, _time(8, 0))
    journey = Journey(
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
    )
    return JourneyResult(status="ok", journeys=(journey,), fetched_at=NOW)


def _time(h: int, m: int):  # local import keeps the fixture section tidy
    from datetime import time as _t

    return _t(h, m)


# ---------------------------------------------------------------------------
# Coordinator builder
# ---------------------------------------------------------------------------


async def _make_coordinator(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    schedules: Mapping[str, FakeSchedule],
    journey: FakeJourney | None = None,
    car: FakeCar | None = None,
) -> tuple[FamilyDeparturesCoordinator, FamilyDeparturesStore]:
    entry.add_to_hass(hass)
    store = FamilyDeparturesStore(hass, entry.entry_id)
    await store.async_load(now=NOW)

    journey = journey or FakeJourney(_transit_result())
    car = car or FakeCar(
        DurationResult(minutes=20.0, fetched_at=NOW, source="waze", quality="realtime")
    )

    def make_schedule(profile: ProfileConfig, ics_url: str | None) -> FakeSchedule:
        return schedules[profile.id]

    factories = ProviderFactories(
        schedule=make_schedule,
        journey=lambda: journey,
        car=lambda: car,
    )
    coordinator = FamilyDeparturesCoordinator(hass, entry, store, factories=factories)
    return coordinator, store


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_transit_profile_plans_and_persists(hass: HomeAssistant) -> None:
    """A public-transport profile gets a plan that is stored (spec §6.1)."""
    entry = _entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": FakeSchedule(_ok_schedule())}
    coordinator, store = await _make_coordinator(hass, entry, schedules=schedules)

    data = await coordinator._async_update_data()

    assert set(data) == {"kid_a"}
    result = data["kid_a"]
    assert result.outcome.day_status == "has_event"
    assert result.plan is not None
    assert result.plan.mode == "public_transport"
    assert result.plan.recommended_leave is not None
    # The plan is stamped with the live config revision (spec §11.2).
    assert result.plan.config_revision == coordinator.config_revision
    # And persisted for restart reasoning (spec §11.3).
    mission_id = make_mission_id("kid_a", TODAY)
    assert store.get_plan(mission_id) is not None
    assert store.get_mission(mission_id) is not None


async def test_packing_list_built_from_schedule(hass: HomeAssistant) -> None:
    """A matching lesson produces a packing item (spec §5.5)."""
    entry = _entry({"kid_b": _profile_dict("kid_b")})
    schedules = {"kid_b": FakeSchedule(_ok_schedule(summary="Lektion IDRO1000X"))}
    coordinator, _store = await _make_coordinator(hass, entry, schedules=schedules)

    data = await coordinator._async_update_data()

    assert data["kid_b"].packing.items == ("Gympakläder",)


async def test_one_source_error_leaves_others_fine(hass: HomeAssistant) -> None:
    """Kid B's source error does not stop Kid A from planning (spec §15)."""
    entry = _entry(
        {
            "kid_a": _profile_dict("kid_a"),
            "kid_b": _profile_dict("kid_b"),
        }
    )
    schedules = {
        "kid_a": FakeSchedule(_ok_schedule()),
        "kid_b": FakeSchedule(_error_schedule()),
    }
    coordinator, _store = await _make_coordinator(hass, entry, schedules=schedules)

    data = await coordinator._async_update_data()

    assert data["kid_a"].plan is not None
    # Kid B surfaces the error distinctly and gets no plan (spec §5.4).
    assert data["kid_b"].outcome.day_status == "source_error"
    assert data["kid_b"].plan is None


async def test_static_plan_makes_no_network_calls(hass: HomeAssistant) -> None:
    """A static profile never calls Waze or the journey planner (spec §6.4)."""
    entry = _entry(
        {
            "parent_a": _profile_dict(
                "parent_a", default_mode="static", static_minutes=15
            )
        }
    )
    schedules = {"parent_a": FakeSchedule(_ok_schedule())}
    journey = FakeJourney(_transit_result())
    car = FakeCar(
        DurationResult(minutes=20.0, fetched_at=NOW, source="waze", quality="realtime")
    )
    coordinator, _store = await _make_coordinator(
        hass, entry, schedules=schedules, journey=journey, car=car
    )

    data = await coordinator._async_update_data()

    assert data["parent_a"].plan is not None
    assert data["parent_a"].plan.mode == "static"
    assert journey.calls == 0
    assert car.calls == 0


async def test_car_falls_back_to_reserve_minutes(hass: HomeAssistant) -> None:
    """An unavailable Waze duration falls back to reserve minutes (spec §6.3)."""
    entry = _entry(
        {
            "parent_b": _profile_dict(
                "parent_b", default_mode="car", car_fallback_minutes=18
            )
        }
    )
    schedules = {"parent_b": FakeSchedule(_ok_schedule())}
    car = FakeCar(
        DurationResult(
            minutes=None, fetched_at=NOW, source="waze", quality="unavailable"
        )
    )
    coordinator, _store = await _make_coordinator(
        hass, entry, schedules=schedules, car=car
    )

    data = await coordinator._async_update_data()

    plan = data["parent_b"].plan
    assert plan is not None
    assert plan.mode == "car"
    # Fallback used: a usable, estimated-quality plan rather than
    # needs_configuration (spec §6.3).
    assert plan.quality == "estimated"
    assert plan.recommended_leave is not None


async def test_car_without_fallback_needs_configuration(hass: HomeAssistant) -> None:
    """No Waze and no reserve minutes => needs_configuration (spec §6.3)."""
    entry = _entry({"parent_b": _profile_dict("parent_b", default_mode="car")})
    schedules = {"parent_b": FakeSchedule(_ok_schedule())}
    car = FakeCar(
        DurationResult(
            minutes=None, fetched_at=NOW, source="waze", quality="unavailable"
        )
    )
    coordinator, _store = await _make_coordinator(
        hass, entry, schedules=schedules, car=car
    )

    data = await coordinator._async_update_data()

    plan = data["parent_b"].plan
    assert plan is not None
    assert plan.status == "needs_configuration"
    assert plan.recommended_leave is None


async def test_today_override_mode_wins(hass: HomeAssistant) -> None:
    """A today override of mode=car overrides a transit-default profile (§4.3)."""
    from custom_components.family_departures.models import DayOverride

    entry = _entry(
        {"parent_b": _profile_dict("parent_b", default_mode="public_transport")}
    )
    schedules = {"parent_b": FakeSchedule(_ok_schedule())}
    journey = FakeJourney(_transit_result())
    car = FakeCar(
        DurationResult(minutes=20.0, fetched_at=NOW, source="waze", quality="realtime")
    )
    coordinator, store = await _make_coordinator(
        hass, entry, schedules=schedules, journey=journey, car=car
    )
    store.set_override(DayOverride(person_id="parent_b", local_date=TODAY, mode="car"))

    data = await coordinator._async_update_data()

    assert data["parent_b"].plan is not None
    assert data["parent_b"].plan.mode == "car"
    assert car.calls == 1
    assert journey.calls == 0


async def test_tomorrow_preview_does_not_replace_today(hass: HomeAssistant) -> None:
    """The tomorrow preview is stored separately and keeps today's plan (§11.1)."""
    entry = _entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": FakeSchedule(_ok_schedule())}
    coordinator, store = await _make_coordinator(hass, entry, schedules=schedules)

    data = await coordinator._async_update_data()
    today_plan = data["kid_a"].plan
    assert today_plan is not None

    previews = await coordinator.async_update_tomorrow_preview()
    tomorrow = TODAY + timedelta(days=1)
    tomorrow_id = make_mission_id("kid_a", tomorrow)
    today_id = make_mission_id("kid_a", TODAY)

    assert tomorrow_id in previews
    # Today's stored plan is untouched; the preview lives under tomorrow's id.
    assert store.get_plan(today_id) == today_plan
    assert store.get_plan(tomorrow_id) is not None
    assert store.get_plan(tomorrow_id).mission_id == tomorrow_id


async def test_stale_revision_round_is_discarded(hass: HomeAssistant) -> None:
    """A config change mid-round drops that round's results (spec §11.2)."""
    entry = _entry({"kid_a": _profile_dict("kid_a")})

    # A schedule provider that reconfigures the coordinator mid-fetch, bumping
    # the config revision so the in-flight round is for an old revision.
    class ReconfiguringSchedule(FakeSchedule):
        def __init__(self, result: ScheduleResult) -> None:
            super().__init__(result)
            self.coordinator: FamilyDeparturesCoordinator | None = None

        async def async_get_day(self, d: date) -> ScheduleResult:
            if self.coordinator is not None:
                self.coordinator.async_reload_config()
                self.coordinator = None
            return await super().async_get_day(d)

    sched = ReconfiguringSchedule(_ok_schedule())
    schedules = {"kid_a": sched}
    coordinator, store = await _make_coordinator(hass, entry, schedules=schedules)
    # Seed a prior published plan so we can prove it is kept.
    coordinator.data = {}
    sched.coordinator = coordinator

    data = await coordinator._async_update_data()

    # The round was for an old revision: it returns the previously published
    # data (empty here) rather than the fresh-but-stale plans.
    assert data == {}
    # And the stale round never wrote its plan/mission to the store (spec §11.2).
    mission_id = make_mission_id("kid_a", TODAY)
    assert store.get_plan(mission_id) is None
    assert store.get_mission(mission_id) is None


async def test_confirmed_departure_not_reset(hass: HomeAssistant) -> None:
    """A later recompute never resets a confirmed departure (spec §11.2)."""
    entry = _entry({"kid_a": _profile_dict("kid_a")})
    schedules = {"kid_a": FakeSchedule(_ok_schedule())}
    coordinator, store = await _make_coordinator(hass, entry, schedules=schedules)

    mission_id = make_mission_id("kid_a", TODAY)
    store.set_mission(
        MissionState(
            mission_id=mission_id,
            status="departed",
            departed_at=NOW,
            reopened=False,
            notified={},
            first_published_leave=None,
            action_nonce="abc",
        )
    )

    await coordinator._async_update_data()

    mission = store.get_mission(mission_id)
    assert mission is not None
    assert mission.status == "departed"


@pytest.mark.parametrize(
    ("minutes_until_leave", "expected"),
    [
        (180, DEFAULT_UPDATE_INTERVAL),  # > 2 h before
        (90, NEAR_UPDATE_INTERVAL),  # 2 h – 30 min window
        (20, IMMINENT_UPDATE_INTERVAL),  # final 30 min
    ],
)
async def test_update_interval_tightens_in_window(
    hass: HomeAssistant,
    minutes_until_leave: int,
    expected: timedelta,
) -> None:
    """The derived poll interval follows the §11.1 windows."""
    entry = _entry({"kid_a": _profile_dict("kid_a")})
    # Build a plan whose recommended_leave is ``minutes_until_leave`` ahead by
    # anchoring the schedule event accordingly.
    event_start = NOW + timedelta(minutes=minutes_until_leave + 60)
    result = ScheduleResult(
        status="ok",
        events=(
            ScheduleEvent(
                uid="e1",
                summary="Lektion MA",
                start=event_start,
                end=event_start + timedelta(minutes=50),
                source_id="src",
            ),
        ),
        fetched_at=NOW,
    )
    # Anchor the first boarding so the recommended leave lands exactly
    # ``minutes_until_leave`` ahead of NOW. For a single transit leg,
    # recommended = departure − boarding(2) − departure_buffer(5) = dep − 7.
    dep = NOW + timedelta(minutes=minutes_until_leave + 7)
    arr = event_start - timedelta(minutes=5)
    journey = FakeJourney(
        JourneyResult(
            status="ok",
            journeys=(
                Journey(
                    journey_id="t1",
                    legs=(
                        Leg(
                            kind="transit",
                            line="17",
                            planned_departure=dep,
                            planned_arrival=arr,
                        ),
                    ),
                ),
            ),
            fetched_at=NOW,
        )
    )
    schedules = {"kid_a": FakeSchedule(result)}
    coordinator, _store = await _make_coordinator(
        hass, entry, schedules=schedules, journey=journey
    )

    data = await coordinator._async_update_data()
    assert data["kid_a"].plan is not None
    assert data["kid_a"].plan.recommended_leave is not None
    assert coordinator.update_interval == expected


async def test_no_mission_relaxes_to_idle_interval(hass: HomeAssistant) -> None:
    """With no active plan the interval relaxes to idle (spec §11.1)."""
    entry = _entry({"kid_a": _profile_dict("kid_a")})
    # Empty schedule => no requirement, no plan.
    empty = ScheduleResult(status="empty", events=(), fetched_at=NOW)
    schedules = {"kid_a": FakeSchedule(empty)}
    coordinator, _store = await _make_coordinator(hass, entry, schedules=schedules)

    data = await coordinator._async_update_data()

    assert data["kid_a"].plan is None
    assert data["kid_a"].outcome.day_status == "no_schedule"
    assert coordinator.update_interval == IDLE_UPDATE_INTERVAL
