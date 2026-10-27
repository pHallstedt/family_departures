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
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from custom_components.family_departures.calendar_links import profile_day_events
from custom_components.family_departures.const import (
    DATA_HOME_LAT,
    DATA_HOME_LON,
    DATA_HOUSEHOLD_NAME,
    DATA_ICS_URLS,
    DOMAIN,
    FETCH_FAILURE_THRESHOLD,
    ISSUE_REPEATED_FETCH_FAILURE,
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
from custom_components.family_departures.household import migrate_v1_to_v2
from custom_components.family_departures.models import (
    CalendarSource,
    DurationResult,
    EventTags,
    Journey,
    JourneyResult,
    Leg,
    LunchMenu,
    MissionState,
    ScheduleEvent,
    ScheduleResult,
)
from custom_components.family_departures.store import FamilyDeparturesStore
from custom_components.family_departures.timeutil import combine_local, make_mission_id
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

HOME = (59.33, 18.06)
# A fixed morning "now"; local date 2026-10-20 (a Tuesday, inside the mask).
NOW = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
TODAY = date(2026, 10, 20)
NO_TAGS = EventTags()


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
    # The coordinator consumes the v2 household shape; migrate the v1 profile
    # dicts in the same way the config-entry migration does (plan §3).
    data = {
        DATA_HOUSEHOLD_NAME: "Familjen",
        DATA_HOME_LAT: HOME[0],
        DATA_HOME_LON: HOME[1],
        # Every profile is ICS; give each a (fake) URL so the provider factory
        # accepts it. The fake schedule provider ignores the URL.
        DATA_ICS_URLS: {pid: f"https://example.test/{pid}" for pid in profiles},
    }
    new_data, new_options = migrate_v1_to_v2(data, {OPT_PROFILES: dict(profiles)})
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Familjen",
        version=2,
        data=new_data,
        options=new_options,
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

    def make_schedule(calendar: CalendarSource, ics_url: str | None) -> FakeSchedule:
        # The migration keys a profile's calendar as ``<pid>_calendar``; the
        # test's ``schedules`` map is keyed by profile id.
        profile_id = calendar.id.removesuffix("_calendar")
        return schedules[profile_id]

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


# ---------------------------------------------------------------------------
# Shared calendars: one fetch, per-profile evaluation (plan §4.1, §4.5, F5)
# ---------------------------------------------------------------------------


def _shared_calendar_entry(
    *,
    profile_ids: tuple[str, ...],
    attendee_mode: str = "all",
    person_entity_ids: Mapping[str, str] | None = None,
) -> MockConfigEntry:
    """A v2 entry where every profile links the same ``shared`` ICS calendar.

    Built directly in the v2 shape (no migration) so the calendar is genuinely
    shared by one record, which the migration never produces (it keys calendars
    per profile).
    """
    person_entity_ids = person_entity_ids or {}
    locations = {
        f"{pid}_destination": {
            "id": f"{pid}_destination",
            "name": "Mål",
            "lat": 59.4,
            "lon": 18.1,
            "aliases": [],
            "static_minutes": None,
        }
        for pid in profile_ids
    }
    calendars = {
        "shared": {
            "id": "shared",
            "name": "Shared",
            "source_type": "ics",
            "calendar_entity_id": None,
        }
    }
    profiles = {}
    for pid in profile_ids:
        profiles[pid] = {
            "id": pid,
            "name": pid.title(),
            "default_location_id": f"{pid}_destination",
            "default_mode": "public_transport",
            "static_minutes": None,
            "static_label": None,
            "weather_adjust": False,
            "car_fallback_minutes": None,
            "arrival": 5,
            "departure": 5,
            "boarding": 2,
            "parking_and_walk": 3,
            "min_transfer": 5,
            "weekday_mask": [0, 1, 2, 3, 4],
            "packing_rules": [],
            "person_entity_id": person_entity_ids.get(pid),
            "notifications_enabled": False,
            "evening_notice_enabled": True,
            "change_threshold_minutes": 3,
            "quiet_start": "22:00:00",
            "quiet_end": "06:00:00",
            "scripts": {},
            "calendar_links": [
                {
                    "link_id": "main",
                    "calendar_id": "shared",
                    "role": "main",
                    "label": "",
                    "include_patterns": [],
                    "exclude_patterns": [],
                    "attendee_mode": attendee_mode,
                    "location_id": None,
                    "mode": None,
                    "static_minutes": None,
                    "arrival_margin": None,
                    "rules": [],
                    "match_event_location": False,
                    "show_summary": False,
                }
            ],
        }
    data = {
        DATA_HOUSEHOLD_NAME: "Familjen",
        DATA_HOME_LAT: HOME[0],
        DATA_HOME_LON: HOME[1],
        DATA_ICS_URLS: {"shared": "https://example.test/shared"},
    }
    options = {
        OPT_PROFILES: profiles,
        "locations": locations,
        "calendars": calendars,
    }
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Familjen",
        version=2,
        data=data,
        options=options,
    )


async def _make_coordinator_shared(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    schedule: FakeSchedule,
) -> tuple[FamilyDeparturesCoordinator, FamilyDeparturesStore]:
    """Build a coordinator whose single shared calendar uses one fake provider."""
    entry.add_to_hass(hass)
    store = FamilyDeparturesStore(hass, entry.entry_id)
    await store.async_load(now=NOW)

    def make_schedule(calendar: CalendarSource, ics_url: str | None) -> FakeSchedule:
        assert calendar.id == "shared"
        return schedule

    factories = ProviderFactories(
        schedule=make_schedule,
        journey=lambda: FakeJourney(_transit_result()),
        car=lambda: FakeCar(
            DurationResult(
                minutes=20.0, fetched_at=NOW, source="waze", quality="realtime"
            )
        ),
    )
    coordinator = FamilyDeparturesCoordinator(hass, entry, store, factories=factories)
    return coordinator, store


async def test_shared_calendar_fetched_once_per_day_per_round(
    hass: HomeAssistant,
) -> None:
    """Two profiles linking one calendar => one provider call per day per round."""
    entry = _shared_calendar_entry(profile_ids=("kid_a", "kid_b"))
    schedule = FakeSchedule(_ok_schedule())
    coordinator, _store = await _make_coordinator_shared(hass, entry, schedule)

    data = await coordinator._async_update_data()

    # Both profiles planned from the same single fetch.
    assert set(data) == {"kid_a", "kid_b"}
    # Exactly one fetch for today in the round, not one per profile.
    assert schedule.calls == [TODAY]
    # Diagnostics report the calendar once with its fan-out.
    health = coordinator.source_health()["shared"]
    assert health.linked_profile_count == 2
    assert health.external_calls == 1


async def test_shared_calendar_failure_raises_single_repair(
    hass: HomeAssistant,
) -> None:
    """A shared calendar failing >= threshold raises one repair, not one/profile."""
    entry = _shared_calendar_entry(profile_ids=("kid_a", "kid_b"))
    schedule = FakeSchedule(_error_schedule())
    coordinator, _store = await _make_coordinator_shared(hass, entry, schedule)

    for _ in range(FETCH_FAILURE_THRESHOLD):
        await coordinator._async_update_data()

    reg = ir.async_get(hass)
    # One issue, keyed per calendar - not per profile.
    assert (
        reg.async_get_issue(DOMAIN, f"{ISSUE_REPEATED_FETCH_FAILURE}_shared")
        is not None
    )
    assert reg.async_get_issue(DOMAIN, f"{ISSUE_REPEATED_FETCH_FAILURE}_kid_a") is None
    assert reg.async_get_issue(DOMAIN, f"{ISSUE_REPEATED_FETCH_FAILURE}_kid_b") is None
    # Still just one fetch per round even while failing.
    assert schedule.calls == [TODAY, TODAY, TODAY]


async def test_attendee_aliases_include_name_friendly_and_object_id(
    hass: HomeAssistant,
) -> None:
    """Alias resolution covers display name, person friendly name and object id."""
    entry = _shared_calendar_entry(
        profile_ids=("kid_a",),
        person_entity_ids={"kid_a": "person.kid_a"},
    )
    schedule = FakeSchedule(_ok_schedule())
    hass.states.async_set("person.kid_a", "home", {"friendly_name": "Barn A"})
    coordinator, _store = await _make_coordinator_shared(hass, entry, schedule)

    config = coordinator.profile_configs["kid_a"]
    aliases = coordinator.attendee_aliases(config)

    # Display name ("Kid_A"), person object id ("kid_a") and friendly name
    # ("Barn A"), all casefolded. Display name and object id coincide here.
    assert config.name.casefold() in aliases
    assert "kid_a" in aliases
    assert "barn a" in aliases


async def test_shared_calendar_attendee_mode_filters_per_profile(
    hass: HomeAssistant,
) -> None:
    """tagged_or_untagged: a tagged event reaches only the named profile."""
    entry = _shared_calendar_entry(
        profile_ids=("kid_a", "kid_b"),
        attendee_mode="tagged_or_untagged",
        person_entity_ids={"kid_a": "person.kid_a", "kid_b": "person.kid_b"},
    )
    start = combine_local(TODAY, _time(8, 20))
    untagged = ScheduleEvent(
        uid="u",
        summary="Gemensam",
        start=start,
        end=start + timedelta(minutes=50),
        source_id="shared",
    )
    tagged_a = ScheduleEvent(
        uid="t",
        summary="Barn A grej",
        start=start + timedelta(hours=1),
        end=start + timedelta(hours=2),
        source_id="shared",
        tags=EventTags(attendees=("Barn A",)),
    )
    schedule = FakeSchedule(
        ScheduleResult(status="ok", events=(untagged, tagged_a), fetched_at=NOW)
    )
    hass.states.async_set("person.kid_a", "home", {"friendly_name": "Barn A"})
    hass.states.async_set("person.kid_b", "home", {"friendly_name": "Barn B"})
    coordinator, store = await _make_coordinator_shared(hass, entry, schedule)

    await coordinator._async_update_data()

    config_a = coordinator.profile_configs["kid_a"]
    config_b = coordinator.profile_configs["kid_b"]
    events_a = profile_day_events(
        store, config_a, TODAY, coordinator.attendee_aliases(config_a)
    )
    events_b = profile_day_events(
        store, config_b, TODAY, coordinator.attendee_aliases(config_b)
    )
    # Barn A sees both; Barn B sees only the untagged shared event.
    assert {e.uid for e in events_a} == {"u", "t"}
    assert {e.uid for e in events_b} == {"u"}


# ---------------------------------------------------------------------------
# Multi-mission: a main school link plus an extra activity calendar (item 6)
# ---------------------------------------------------------------------------


class RecordingJourney(FakeJourney):
    """A journey provider that also records the destinations it was asked for."""

    def __init__(self, result: JourneyResult) -> None:
        super().__init__(result)
        self.destinations: list[tuple[float, float]] = []

    async def async_plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        deadline: datetime,
        earliest_departure: datetime | None,
    ) -> JourneyResult:
        self.destinations.append(destination)
        return await super().async_plan(
            origin, destination, deadline, earliest_departure
        )


def _link(link_id: str, calendar_id: str, role: str, **extra: Any) -> dict[str, Any]:
    return {
        "link_id": link_id,
        "calendar_id": calendar_id,
        "role": role,
        "label": extra.pop("label", ""),
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


def _activity_entry(school_link: Mapping[str, Any] | None = None) -> MockConfigEntry:
    """One profile: ``school`` main link and ``club`` extra link (own calendars)."""
    profile = {
        "id": "kid_a",
        "name": "Kid A",
        "default_location_id": "school",
        "default_mode": "public_transport",
        "static_minutes": None,
        "static_label": None,
        "weather_adjust": False,
        "car_fallback_minutes": None,
        "arrival": 5,
        "departure": 5,
        "boarding": 2,
        "parking_and_walk": 3,
        "min_transfer": 5,
        "weekday_mask": [0, 1, 2, 3, 4],
        "packing_rules": [],
        "person_entity_id": None,
        "notifications_enabled": False,
        "evening_notice_enabled": True,
        "change_threshold_minutes": 3,
        "quiet_start": "22:00:00",
        "quiet_end": "06:00:00",
        "scripts": {},
        "calendar_links": [
            _link("school", "school_cal", "main", **(school_link or {})),
            _link("club", "club_cal", "extra", label="Fotboll"),
        ],
    }
    options = {
        OPT_PROFILES: {"kid_a": profile},
        "locations": {
            "school": {"id": "school", "name": "Skolan", "lat": 59.4, "lon": 18.1},
            "pitch": {
                "id": "pitch",
                "name": "Fotbollsplanen",
                "lat": 59.5,
                "lon": 18.2,
                "aliases": ["Plan 3"],
            },
        },
        "calendars": {
            "school_cal": {"id": "school_cal", "name": "Skolan", "source_type": "ics"},
            "club_cal": {"id": "club_cal", "name": "Klubben", "source_type": "ics"},
        },
    }
    data = {
        DATA_HOUSEHOLD_NAME: "Familjen",
        DATA_HOME_LAT: HOME[0],
        DATA_HOME_LON: HOME[1],
        DATA_ICS_URLS: {
            "school_cal": "https://example.test/school",
            "club_cal": "https://example.test/club",
        },
    }
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Familjen",
        version=2,
        data=data,
        options=options,
    )


def _event_at(
    uid: str,
    hh: int,
    mm: int,
    *,
    minutes: int = 60,
    summary: str = "Träning",
    key: str | None = None,
    tags: EventTags = NO_TAGS,
) -> ScheduleEvent:
    start = combine_local(TODAY, _time(hh, mm))
    return ScheduleEvent(
        uid=uid,
        summary=summary,
        start=start,
        end=start + timedelta(minutes=minutes),
        source_id="src",
        occurrence_key=key,
        tags=tags,
    )


def _day(*events: ScheduleEvent) -> ScheduleResult:
    return ScheduleResult(
        status="ok" if events else "empty", events=events, fetched_at=NOW
    )


async def _make_activity_coordinator(
    hass: HomeAssistant,
    school: FakeSchedule,
    club: FakeSchedule,
    journey: FakeJourney,
    *,
    school_link: Mapping[str, Any] | None = None,
) -> tuple[FamilyDeparturesCoordinator, FamilyDeparturesStore]:
    entry = _activity_entry(school_link)
    entry.add_to_hass(hass)
    store = FamilyDeparturesStore(hass, entry.entry_id)
    await store.async_load(now=NOW)
    providers = {"school_cal": school, "club_cal": club}
    factories = ProviderFactories(
        schedule=lambda calendar, ics_url: providers[calendar.id],
        journey=lambda: journey,
        car=lambda: FakeCar(
            DurationResult(
                minutes=20.0, fetched_at=NOW, source="waze", quality="realtime"
            )
        ),
    )
    return FamilyDeparturesCoordinator(hass, entry, store, factories=factories), store


async def test_extra_events_become_planned_missions(hass: HomeAssistant) -> None:
    school = FakeSchedule(_day(_event_at("lesson", 8, 20, summary="Lektion")))
    club = FakeSchedule(
        _day(
            _event_at("f1", 17, 30, key="k1"),
            _event_at("f2", 19, 30, key="k2", tags=EventTags(location="Plan 3")),
        )
    )
    journey = RecordingJourney(_transit_result())
    coordinator, store = await _make_activity_coordinator(hass, school, club, journey)

    data = await coordinator._async_update_data()

    result = data["kid_a"]
    assert [m.requirement.slot for m in result.missions] == [
        "morning",
        "x-club-k1",
        "x-club-k2",
    ]
    # The school mission is active until it is done; the sensors follow it.
    assert result.plan is not None
    assert result.plan.mission_id == "kid_a:2026-10-20:morning"
    assert result.outcome.day_status == "has_event"
    # Every mission is planned against its own destination (tag -> pitch).
    assert journey.destinations == [(59.4, 18.1), (59.4, 18.1), (59.5, 18.2)]
    # All missions are persisted with state and plan.
    for mission in result.missions:
        assert store.get_mission(mission.plan.mission_id) is not None
        assert store.get_plan(mission.plan.mission_id) is not None
    # Each calendar is cached once, unfiltered.
    assert store.get_schedule("club_cal", TODAY) is not None
    assert school.calls == [TODAY]
    assert club.calls == [TODAY]


async def test_active_mission_moves_to_next_after_departure(
    hass: HomeAssistant,
) -> None:
    school = FakeSchedule(_day(_event_at("lesson", 8, 20)))
    club = FakeSchedule(_day(_event_at("f1", 17, 30, key="k1")))
    coordinator, store = await _make_activity_coordinator(
        hass, school, club, RecordingJourney(_transit_result())
    )
    await coordinator._async_update_data()
    morning = store.get_mission("kid_a:2026-10-20:morning")
    assert morning is not None
    store.set_mission(
        MissionState(
            mission_id=morning.mission_id,
            status="departed",
            departed_at=NOW,
            reopened=False,
            notified=morning.notified,
            first_published_leave=morning.first_published_leave,
            action_nonce=morning.action_nonce,
        )
    )

    data = await coordinator._async_update_data()

    plan = data["kid_a"].plan
    assert plan is not None
    assert plan.mission_id == "kid_a:2026-10-20:x-club-k1"
    assert data["kid_a"].outcome.requirement == plan.requirement


async def test_far_extra_mission_replan_is_throttled(hass: HomeAssistant) -> None:
    school = FakeSchedule(_day(_event_at("lesson", 8, 20)))
    club = FakeSchedule(_day(_event_at("f1", 19, 30, key="k1")))
    journey = RecordingJourney(_transit_result())
    coordinator, _store = await _make_activity_coordinator(hass, school, club, journey)

    await coordinator._async_update_data()
    assert journey.calls == 2  # school + football
    await coordinator._async_update_data()

    # The football event is >4 h away: only the school mission is re-planned.
    assert journey.calls == 3


async def test_extra_calendar_failure_leaves_main_and_is_reported(
    hass: HomeAssistant,
) -> None:
    school = FakeSchedule(_day(_event_at("lesson", 8, 20)))
    club = FakeSchedule(_error_schedule())
    coordinator, _store = await _make_activity_coordinator(
        hass, school, club, RecordingJourney(_transit_result())
    )

    data = await coordinator._async_update_data()

    result = data["kid_a"]
    assert [m.requirement.slot for m in result.missions] == ["morning"]
    assert result.failed_extra_links == ("club",)
    assert "extra_source_error" in result.outcome.reason_codes


async def test_back_to_back_activity_is_flagged(hass: HomeAssistant) -> None:
    school = FakeSchedule(_day(_event_at("last", 16, 10)))  # ends 17:10
    club = FakeSchedule(_day(_event_at("f1", 17, 30, key="k1")))
    coordinator, _store = await _make_activity_coordinator(
        hass, school, club, RecordingJourney(_transit_result())
    )

    data = await coordinator._async_update_data()

    football = data["kid_a"].missions[1]
    assert "home_gap_too_short" in football.requirement.warnings
    assert "home_gap_too_short" in football.plan.reason_codes


async def test_main_link_exclude_patterns_apply_to_planning(
    hass: HomeAssistant,
) -> None:
    """The main link's own filters decide which event is the first lesson."""
    school = FakeSchedule(
        _day(
            _event_at("rast", 8, 0, summary="Rast"),
            _event_at("lesson", 8, 20, summary="Lektion"),
        )
    )
    coordinator, _store = await _make_activity_coordinator(
        hass,
        school,
        FakeSchedule(_day()),
        RecordingJourney(_transit_result()),
        school_link={"exclude_patterns": ["Rast"]},
    )

    data = await coordinator._async_update_data()

    requirement = data["kid_a"].missions[0].requirement
    assert requirement.event_id == "lesson"
    assert requirement.event_start == combine_local(TODAY, _time(8, 20))


async def test_skip_shows_at_once_despite_the_replan_throttle(
    hass: HomeAssistant,
) -> None:
    """A skipped far-away extra mission is not served from the throttled plan."""
    school = FakeSchedule(_day(_event_at("lesson", 8, 20)))
    club = FakeSchedule(_day(_event_at("f1", 19, 30, key="k1")))
    journey = RecordingJourney(_transit_result())
    coordinator, store = await _make_activity_coordinator(hass, school, club, journey)
    await coordinator._async_update_data()
    football_id = "kid_a:2026-10-20:x-club-k1"
    state = store.get_mission(football_id)
    assert state is not None
    store.set_mission(
        MissionState(
            mission_id=football_id,
            status="skipped",
            departed_at=None,
            reopened=False,
            notified=state.notified,
            first_published_leave=state.first_published_leave,
            action_nonce=state.action_nonce,
        )
    )
    calls = journey.calls

    data = await coordinator._async_update_data()

    football = next(
        m for m in data["kid_a"].missions if m.requirement.slot != "morning"
    )
    assert football.plan.status == "skipped"
    # It was re-planned (the throttle was bypassed), not reused.
    assert journey.calls == calls + 2


class RaisingSchedule:
    """A provider that crashes instead of returning an error result."""

    async def async_get_day(self, d: date) -> ScheduleResult:
        raise RuntimeError("boom")


async def test_crashing_calendar_provider_raises_the_repeated_failure_repair(
    hass: HomeAssistant,
) -> None:
    """A calendar whose provider keeps raising must count as failing."""
    entry = _shared_calendar_entry(profile_ids=("kid_a",))
    coordinator, _store = await _make_coordinator_shared(
        hass,
        entry,
        RaisingSchedule(),  # type: ignore[arg-type]
    )

    for _ in range(FETCH_FAILURE_THRESHOLD):
        await coordinator._async_update_data()

    reg = ir.async_get(hass)
    assert (
        reg.async_get_issue(DOMAIN, f"{ISSUE_REPEATED_FETCH_FAILURE}_shared")
        is not None
    )


async def test_stale_conflict_warning_does_not_survive_the_replan_throttle(
    hass: HomeAssistant,
) -> None:
    """Once the overlapping event is gone its warning must clear, even for a
    far-away extra mission served from the throttled stored plan."""
    school = FakeSchedule(_day(_event_at("lesson", 8, 20)))
    club = FakeSchedule(
        _day(_event_at("f1", 19, 30, key="k1"), _event_at("f2", 19, 45, key="k2"))
    )
    coordinator, _store = await _make_activity_coordinator(
        hass, school, club, RecordingJourney(_transit_result())
    )

    data = await coordinator._async_update_data()
    first = next(m for m in data["kid_a"].missions if m.requirement.slot == "x-club-k1")
    assert "overlaps_other_commitment" in first.requirement.warnings

    club.result = _day(_event_at("f1", 19, 30, key="k1"))  # the overlap is gone
    data = await coordinator._async_update_data()

    first = next(m for m in data["kid_a"].missions if m.requirement.slot == "x-club-k1")
    # (The fake journey always leaves at 07:30, so the home-gap flag is
    # legitimately recomputed; only the overlap flag must be gone.)
    assert "overlaps_other_commitment" not in first.requirement.warnings
    assert "overlaps_other_commitment" not in first.plan.reason_codes


async def test_changing_a_link_filter_is_not_reported_as_a_removed_event(
    hass: HomeAssistant,
) -> None:
    """An event that only drops out of the profile's view (a link filter was
    edited) is still in the calendar: no "removed" notice."""
    from custom_components.family_departures.models import (
        NotifiedRecord,
        SourceFilter,
    )
    from custom_components.family_departures.notification_policy import (
        KIND_MORNING,
    )
    from custom_components.family_departures.scheduler import NotificationScheduler

    school = FakeSchedule(_day(_event_at("lesson", 8, 20)))
    club = FakeSchedule(_day(_event_at("f1", 17, 30, summary="Träning", key="k1")))
    coordinator, store = await _make_activity_coordinator(
        hass, school, club, RecordingJourney(_transit_result())
    )
    await coordinator._async_update_data()
    football_id = "kid_a:2026-10-20:x-club-k1"
    state = store.get_mission(football_id)
    assert state is not None
    store.set_mission(
        replace(
            state,
            notified={
                KIND_MORNING: NotifiedRecord(
                    kind=KIND_MORNING, sent_at=NOW, leave_time=None, revision=1
                )
            },
        )
    )
    runtime = coordinator._runtimes["kid_a"]
    links = tuple(
        replace(link, source_filter=SourceFilter(exclude_patterns=("Träning",)))
        if link.link_id == "club"
        else link
        for link in runtime.config.calendar_links
    )
    runtime.config = replace(
        runtime.config, calendar_links=links, notifications_enabled=True
    )
    coordinator.data = await coordinator._async_update_data()
    assert football_id not in {
        m.plan.mission_id for m in coordinator.data["kid_a"].missions
    }

    sent: list[Any] = []

    async def _dispatch(intents: list[Any]) -> None:
        sent.extend(intents)

    scheduler = NotificationScheduler(hass, coordinator, store, _dispatch)  # type: ignore[arg-type]
    removed = scheduler._removed_missions(scheduler._current_plans(), NOW)
    await scheduler._async_notify_removed(removed, NOW)

    assert [i.kind for i in sent] == []


async def test_issues_and_health_of_a_deleted_calendar_are_cleared(
    hass: HomeAssistant,
) -> None:
    entry = _shared_calendar_entry(profile_ids=("kid_a",))
    coordinator, _store = await _make_coordinator_shared(
        hass, entry, FakeSchedule(_error_schedule())
    )
    for _ in range(FETCH_FAILURE_THRESHOLD):
        await coordinator._async_update_data()
    reg = ir.async_get(hass)
    issue_id = f"{ISSUE_REPEATED_FETCH_FAILURE}_shared"
    assert reg.async_get_issue(DOMAIN, issue_id) is not None

    # The calendar (and the profile linking it) is deleted from the household.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, OPT_PROFILES: {}, "calendars": {}}
    )
    coordinator.async_reload_config()
    await coordinator._async_update_data()

    assert reg.async_get_issue(DOMAIN, issue_id) is None
    assert "shared" not in coordinator.source_health()


# ---------------------------------------------------------------------------
# Lunch (plan §7.2)
# ---------------------------------------------------------------------------


class _FakeLunch:
    def __init__(self, status: str = "ok", *, raises: bool = False) -> None:
        self.status = status
        self.raises = raises
        self.calls: list[date] = []

    async def async_get_lunch(self, d: date) -> LunchMenu:
        self.calls.append(d)
        if self.raises:
            raise RuntimeError("boom")
        dishes = ("Pasta",) if self.status == "ok" else ()
        return LunchMenu(
            local_date=d,
            status=self.status,
            dishes=dishes,
            fetched_at=NOW,  # type: ignore[arg-type]
        )


async def _lunch_coordinator(
    hass: HomeAssistant, lunch: _FakeLunch, *, entities: Mapping[str, str | None]
) -> FamilyDeparturesCoordinator:
    profiles = {}
    for pid, entity_id in entities.items():
        profile = _profile_dict(pid)
        profile["lunch_calendar_entity_id"] = entity_id
        profiles[pid] = profile
    schedules = {pid: FakeSchedule(_ok_schedule()) for pid in profiles}
    coordinator, _store = await _make_coordinator(
        hass, _entry(profiles), schedules=schedules
    )
    coordinator._factories.lunch = lambda entity_id: lunch
    return coordinator


async def test_two_profiles_on_one_lunch_entity_make_one_call(
    hass: HomeAssistant,
) -> None:
    lunch = _FakeLunch()
    coordinator = await _lunch_coordinator(
        hass, lunch, entities={"kid_a": "calendar.skolmat", "kid_b": "calendar.skolmat"}
    )

    data = await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert lunch.calls == [TODAY + timedelta(days=1)]
    for pid in ("kid_a", "kid_b"):
        menu = data[pid].lunch_tomorrow
        assert menu is not None and menu.dishes == ("Pasta",)


async def test_a_good_menu_is_refetched_after_three_hours_an_error_after_thirty_minutes(
    hass: HomeAssistant,
) -> None:
    good = _FakeLunch()
    coordinator = await _lunch_coordinator(
        hass, good, entities={"kid_a": "calendar.skolmat"}
    )
    config = coordinator.profile_configs["kid_a"]
    await coordinator._lunch_tomorrow(config, TODAY, NOW)
    await coordinator._lunch_tomorrow(
        config, TODAY, NOW + timedelta(hours=2, minutes=59)
    )
    assert len(good.calls) == 1
    await coordinator._lunch_tomorrow(config, TODAY, NOW + timedelta(hours=3))
    assert len(good.calls) == 2

    bad = _FakeLunch("error")
    coordinator = await _lunch_coordinator(
        hass, bad, entities={"kid_a": "calendar.skolmat"}
    )
    config = coordinator.profile_configs["kid_a"]
    await coordinator._lunch_tomorrow(config, TODAY, NOW)
    await coordinator._lunch_tomorrow(config, TODAY, NOW + timedelta(minutes=29))
    assert len(bad.calls) == 1
    await coordinator._lunch_tomorrow(config, TODAY, NOW + timedelta(minutes=30))
    assert len(bad.calls) == 2


async def test_a_crashing_lunch_provider_gives_an_error_menu_not_a_failed_profile(
    hass: HomeAssistant,
) -> None:
    coordinator = await _lunch_coordinator(
        hass, _FakeLunch(raises=True), entities={"kid_a": "calendar.skolmat"}
    )

    data = await coordinator._async_update_data()

    assert data["kid_a"].plan is not None
    menu = data["kid_a"].lunch_tomorrow
    assert menu is not None and menu.status == "error"


async def test_no_lunch_entity_means_no_menu_and_no_call(hass: HomeAssistant) -> None:
    lunch = _FakeLunch()
    coordinator = await _lunch_coordinator(hass, lunch, entities={"kid_a": None})

    data = await coordinator._async_update_data()

    assert data["kid_a"].lunch_tomorrow is None
    assert lunch.calls == []
