"""Notifications through a real config entry (spec §11.3, §12.1, §12.2).

These tests set up the integration with ``hass.config_entries.async_setup``,
exactly as Home Assistant does, with only the schedule/travel providers faked.
They prove the notification pipeline is wired end to end: the scheduler arms
timers at setup, the dispatcher fires the ``family_departures_notification``
hook event and calls the profile's mapped channel script, dry-run suppresses
the script, unload/reload neither leaks timers nor resends, the 20:00 evening
notice uses tomorrow's preview with its packing list, an earlier departure
alerts once it is stable over two rounds, and a profile that fails its first
refresh still gets (unavailable) entities that recover without a reload.

The clock is driven with the ``freezer`` fixture plus
``async_fire_time_changed``. Fixtures are synthetic: generic coordinates,
``example.test`` URLs, course-code summaries and ``kid_*`` profile ids.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from custom_components.family_departures import coordinator as coordinator_mod
from custom_components.family_departures.const import (
    DATA_HOME_LAT,
    DATA_HOME_LON,
    DATA_HOUSEHOLD_NAME,
    DATA_ICS_URLS,
    DOMAIN,
    EVENT_NOTIFICATION,
    OPT_DRY_RUN,
    OPT_PROFILES,
)
from custom_components.family_departures.coordinator import ProviderFactories
from custom_components.family_departures.models import (
    DurationResult,
    JourneyResult,
    ProfileConfig,
    ScheduleEvent,
    ScheduleResult,
)
from custom_components.family_departures.timeutil import TZ, combine_local
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
    async_mock_service,
)

HOME = (59.33, 18.06)
MONDAY = date(2026, 10, 19)
TUESDAY = date(2026, 10, 20)
PUSH_SCRIPT = "script.fd_push_kid_a"


def _local(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=TZ)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSchedule:
    """Date-aware schedule: one lesson per configured day, else an empty day."""

    def __init__(
        self, summaries: Mapping[date, str], *, start: time = time(8, 20)
    ) -> None:
        self.summaries = dict(summaries)
        self.start = start
        self.fail = False

    async def async_get_day(self, d: date) -> ScheduleResult:
        if self.fail:
            raise RuntimeError("simulated provider crash")
        now = dt_util.utcnow()
        summary = self.summaries.get(d)
        if summary is None:
            return ScheduleResult(status="empty", events=(), fetched_at=now)
        start = combine_local(d, self.start)
        return ScheduleResult(
            status="ok",
            events=(
                ScheduleEvent(
                    uid=f"e-{d.isoformat()}",
                    summary=summary,
                    start=start,
                    end=start + timedelta(minutes=50),
                    source_id="unused",
                ),
            ),
            fetched_at=now,
        )


class _FakeCar:
    """Car provider whose travel minutes the test can change between rounds."""

    def __init__(self, minutes: float) -> None:
        self.minutes = minutes

    async def async_get_duration(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        realtime: bool,
        time_delta: timedelta | None,
    ) -> DurationResult:
        return DurationResult(
            minutes=self.minutes,
            fetched_at=dt_util.utcnow(),
            source="waze",
            quality="realtime",
        )


class _NoJourney:
    async def async_plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        deadline: datetime,
        earliest_departure: datetime | None,
    ) -> JourneyResult:
        return JourneyResult(status="empty", journeys=(), fetched_at=dt_util.utcnow())


def _patch_factories(
    schedules: Mapping[str, _FakeSchedule], *, car: _FakeCar | None = None
) -> Any:
    car = car or _FakeCar(20)
    journey = _NoJourney()

    def make_schedule(profile: ProfileConfig, ics_url: str | None) -> _FakeSchedule:
        return schedules[profile.id]

    def _factory(hass: HomeAssistant) -> ProviderFactories:
        return ProviderFactories(
            schedule=make_schedule, journey=lambda: journey, car=lambda: car
        )

    return patch.object(coordinator_mod, "_default_factories", _factory)


# ---------------------------------------------------------------------------
# Entry builders
# ---------------------------------------------------------------------------


def _profile_dict(profile_id: str, **overrides: Any) -> dict[str, Any]:
    """A static-mode profile: 15 min door to door, arrival 5, departure 5.

    Event 08:20 → deadline 08:15 → latest 08:00 → recommended 07:55, so the
    morning notice is due 06:55 and the reminder 07:45.
    """
    profile: dict[str, Any] = {
        "id": profile_id,
        "name": profile_id.title(),
        "source_type": "ics",
        "calendar_entity_id": None,
        "exclude_patterns": [],
        "include_patterns": [],
        "destination_id": f"{profile_id}_destination",
        "dest_lat": 59.4,
        "dest_lon": 18.1,
        "default_mode": "static",
        "static_minutes": 15,
        "static_label": None,
        "weather_adjust": False,
        "car_fallback_minutes": None,
        "arrival": 5,
        "departure": 5,
        "boarding": 2,
        "parking_and_walk": 0,
        "min_transfer": 5,
        "weekday_mask": ["0", "1", "2", "3", "4"],
        "packing_rules": [{"id": "r1", "match": "IDRO", "item": "Gympakläder"}],
        "person_entity_id": None,
        "notifications_enabled": True,
        "evening_notice_enabled": True,
        "change_threshold_minutes": 3,
        "quiet_start": "22:00:00",
        "quiet_end": "06:00:00",
        "scripts": {"push": f"script.fd_push_{profile_id}"},
    }
    profile.update(overrides)
    return profile


def _make_entry(
    profiles: Mapping[str, dict[str, Any]], **options: Any
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Familjen",
        data={
            DATA_HOUSEHOLD_NAME: "Familjen",
            DATA_HOME_LAT: HOME[0],
            DATA_HOME_LON: HOME[1],
            DATA_ICS_URLS: {pid: f"https://example.test/{pid}" for pid in profiles},
        },
        options={OPT_PROFILES: dict(profiles), **options},
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _advance(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, when: datetime
) -> None:
    freezer.move_to(when)
    async_fire_time_changed(hass, when)
    await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str:
    ent_reg = er.async_get(hass)
    for domain in ("sensor", "binary_sensor", "select", "switch", "button"):
        entity_id = ent_reg.async_get_entity_id(
            domain, DOMAIN, f"{entry.entry_id}_{key}"
        )
        if entity_id is not None:
            return entity_id
    raise AssertionError(f"no entity for unique id suffix {key!r}")


def _kinds(events: list[Event]) -> list[str]:
    return [event.data["kind"] for event in events]


def _script_kinds(calls: list[ServiceCall]) -> list[str]:
    return [call.data["variables"]["kind"] for call in calls]


# ---------------------------------------------------------------------------
# Bug 1: the scheduler and dispatcher are wired at setup
# ---------------------------------------------------------------------------


async def test_reminder_is_delivered_to_the_mapped_script(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """At recommended −10 min the reminder reaches the profile's push script."""
    events = async_capture_events(hass, EVENT_NOTIFICATION)
    calls = async_mock_service(hass, "script", "turn_on")
    freezer.move_to(_local(TUESDAY, 6, 30))
    entry = _make_entry({"kid_a": _profile_dict("kid_a")}, **{OPT_DRY_RUN: False})
    schedules = {"kid_a": _FakeSchedule({TUESDAY: "Lektion MA"})}
    with _patch_factories(schedules):
        await _setup(hass, entry)
        state = hass.states.get(_entity_id(hass, entry, "kid_a_recommended_leave_time"))
        assert state is not None
        assert datetime.fromisoformat(state.state) == _local(TUESDAY, 7, 55)

        await _advance(hass, freezer, _local(TUESDAY, 7, 45))

    assert "reminder" in _kinds(events)
    reminder_calls = [
        call for call in calls if call.data["variables"]["kind"] == "reminder"
    ]
    assert len(reminder_calls) == 1
    assert reminder_calls[0].data["entity_id"] == PUSH_SCRIPT


@pytest.mark.parametrize(
    "options",
    [{OPT_DRY_RUN: True}, {}],
    ids=["dry_run_on", "dry_run_never_saved_defaults_on"],
)
async def test_dry_run_fires_event_but_calls_no_script(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, options: dict[str, Any]
) -> None:
    """Dry run (also the default) fires the hook event but calls no script."""
    events = async_capture_events(hass, EVENT_NOTIFICATION)
    calls = async_mock_service(hass, "script", "turn_on")
    freezer.move_to(_local(TUESDAY, 6, 30))
    entry = _make_entry({"kid_a": _profile_dict("kid_a")}, **options)
    schedules = {"kid_a": _FakeSchedule({TUESDAY: "Lektion MA"})}
    with _patch_factories(schedules):
        await _setup(hass, entry)
        await _advance(hass, freezer, _local(TUESDAY, 7, 45))

    assert "reminder" in _kinds(events)
    assert calls == []


async def test_unload_and_reload_do_not_duplicate_or_leak_notifications(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A reload does not resend a sent notice; after unload nothing fires."""
    events = async_capture_events(hass, EVENT_NOTIFICATION)
    calls = async_mock_service(hass, "script", "turn_on")
    freezer.move_to(_local(TUESDAY, 6, 30))
    entry = _make_entry({"kid_a": _profile_dict("kid_a")}, **{OPT_DRY_RUN: False})
    schedules = {"kid_a": _FakeSchedule({TUESDAY: "Lektion MA"})}
    with _patch_factories(schedules):
        await _setup(hass, entry)
        await _advance(hass, freezer, _local(TUESDAY, 6, 55))
        assert _kinds(events) == ["morning"]

        # Reload right away, before the store's debounced save would run.
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        await _advance(hass, freezer, _local(TUESDAY, 7, 45))

        assert _kinds(events).count("morning") == 1
        assert _kinds(events).count("reminder") == 1
        assert _script_kinds(calls) == _kinds(events)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        sent_events, sent_calls = len(events), len(calls)

        await _advance(hass, freezer, _local(TUESDAY, 7, 55))
        await _advance(hass, freezer, _local(TUESDAY, 8, 30))

    assert len(events) == sent_events
    assert len(calls) == sent_calls


# ---------------------------------------------------------------------------
# Bug 2: evening notice and tomorrow's packing list
# ---------------------------------------------------------------------------


async def test_evening_notice_fires_at_20_for_tomorrow_with_packing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """At 20:00 the evening notice for tomorrow carries tomorrow's packing."""
    events = async_capture_events(hass, EVENT_NOTIFICATION)
    calls = async_mock_service(hass, "script", "turn_on")
    freezer.move_to(_local(MONDAY, 19, 0))
    entry = _make_entry({"kid_a": _profile_dict("kid_a")}, **{OPT_DRY_RUN: False})
    schedules = {
        "kid_a": _FakeSchedule({MONDAY: "Lektion MA", TUESDAY: "Lektion IDRO1000X"})
    }
    with _patch_factories(schedules):
        await _setup(hass, entry)

        tomorrow = hass.states.get(
            _entity_id(hass, entry, "kid_a_packing_list_tomorrow")
        )
        assert tomorrow is not None
        assert tomorrow.state == "Gympakläder"
        assert tomorrow.attributes["items"] == ["Gympakläder"]
        today = hass.states.get(_entity_id(hass, entry, "kid_a_packing_list"))
        assert today is not None
        assert today.state == ""

        await _advance(hass, freezer, _local(MONDAY, 19, 30))
        assert events == []
        await _advance(hass, freezer, _local(MONDAY, 20, 0))

    # Exactly the evening notice; nothing stale for Monday's finished mission.
    assert _kinds(events) == ["evening"]
    evening = events[0].data
    assert evening["notification_id"].startswith("kid_a:2026-10-20:morning")
    assert "Gympakläder" in evening["packing_items"]
    assert _script_kinds(calls) == ["evening"]
    assert calls[0].data["entity_id"] == PUSH_SCRIPT


# ---------------------------------------------------------------------------
# Bug 3: an earlier departure alerts once stable over two rounds
# ---------------------------------------------------------------------------


async def test_earlier_departure_alerts_after_two_stable_rounds(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A 10-min earlier departure alerts on the second round that confirms it."""
    events = async_capture_events(hass, EVENT_NOTIFICATION)
    async_mock_service(hass, "script", "turn_on")
    freezer.move_to(_local(TUESDAY, 6, 30))
    profile = _profile_dict(
        "kid_a", default_mode="car", static_minutes=None, parking_and_walk=3
    )
    entry = _make_entry({"kid_a": profile}, **{OPT_DRY_RUN: False})
    # Event 09:00 → deadline 08:55; car 20 + parking 3 → latest 08:32, rec 08:27.
    schedules = {"kid_a": _FakeSchedule({TUESDAY: "Lektion MA"}, start=time(9, 0))}
    car = _FakeCar(20)
    with _patch_factories(schedules, car=car):
        await _setup(hass, entry)
        coordinator = entry.runtime_data.coordinator
        assert coordinator.data["kid_a"].plan.recommended_leave == _local(
            TUESDAY, 8, 27
        )

        # Traffic now says 30 min → recommended 08:17 (10 min earlier).
        car.minutes = 30
        freezer.move_to(_local(TUESDAY, 6, 35))
        await coordinator.async_refresh()
        await _advance(hass, freezer, _local(TUESDAY, 6, 36))
        assert "change" not in _kinds(events)

        # A second round confirms the same earlier time.
        freezer.move_to(_local(TUESDAY, 6, 40))
        await coordinator.async_refresh()
        await _advance(hass, freezer, _local(TUESDAY, 6, 41))

    changes = [event.data for event in events if event.data["kind"] == "change"]
    assert len(changes) == 1
    assert "leave_time_improved" in changes[0]["reason_codes"]
    assert datetime.fromisoformat(changes[0]["recommended_leave_time"]) == _local(
        TUESDAY, 8, 17
    )


async def test_tomorrow_preview_sends_only_the_evening_notice(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Evening traffic swings in tomorrow's car preview send no change alerts.

    The preview follows live evening traffic, so only the 20:00 evening notice
    goes out for tomorrow; change alerts wait for the trip's own day.
    """
    events = async_capture_events(hass, EVENT_NOTIFICATION)
    calls = async_mock_service(hass, "script", "turn_on")
    freezer.move_to(_local(MONDAY, 18, 0))
    profile = _profile_dict("kid_a", default_mode="car", static_minutes=None)
    entry = _make_entry({"kid_a": profile}, **{OPT_DRY_RUN: False})
    schedules = {"kid_a": _FakeSchedule({TUESDAY: "Lektion MA"})}
    car = _FakeCar(32)
    with _patch_factories(schedules, car=car):
        await _setup(hass, entry)
        coordinator = entry.runtime_data.coordinator
        preview = coordinator.data["kid_a"].tomorrow_plan
        assert preview is not None
        # Deadline 08:15 − 32 min → latest 07:43, recommended 07:38.
        assert preview.recommended_leave == _local(TUESDAY, 7, 38)

        # 18:30: traffic eases to 24 min → recommended 07:46 (+8 min).
        car.minutes = 24
        freezer.move_to(_local(MONDAY, 18, 30))
        await coordinator.async_refresh()
        await _advance(hass, freezer, _local(MONDAY, 18, 31))
        assert events == []

        await _advance(hass, freezer, _local(MONDAY, 20, 0))
        assert _kinds(events) == ["evening"]

        # 20:30: 20 min → recommended 07:50, +4 vs the evening notice.
        car.minutes = 20
        freezer.move_to(_local(MONDAY, 20, 30))
        await coordinator.async_refresh()
        await _advance(hass, freezer, _local(MONDAY, 20, 31))

    assert _kinds(events) == ["evening"]
    assert _script_kinds(calls) == ["evening"]


# ---------------------------------------------------------------------------
# Go now when a round moves the departure earlier past now
# ---------------------------------------------------------------------------


async def test_go_now_is_sent_when_a_round_moves_departure_past_now(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Worse traffic right before leaving still gets "dags att gå" in time.

    07:45 reminder for 07:55 (latest 08:00); at 07:50 traffic moves the
    recommended departure to 07:46 with latest 07:51, so the trip is still on
    time and the go-now must go out.
    """
    events = async_capture_events(hass, EVENT_NOTIFICATION)
    calls = async_mock_service(hass, "script", "turn_on")
    freezer.move_to(_local(TUESDAY, 6, 30))
    profile = _profile_dict("kid_a", default_mode="car", static_minutes=None)
    entry = _make_entry({"kid_a": profile}, **{OPT_DRY_RUN: False})
    schedules = {"kid_a": _FakeSchedule({TUESDAY: "Lektion MA"})}
    car = _FakeCar(15)
    with _patch_factories(schedules, car=car):
        await _setup(hass, entry)
        coordinator = entry.runtime_data.coordinator
        assert coordinator.data["kid_a"].plan.recommended_leave == _local(
            TUESDAY, 7, 55
        )
        await _advance(hass, freezer, _local(TUESDAY, 7, 45))
        assert "reminder" in _kinds(events)

        car.minutes = 24
        freezer.move_to(_local(TUESDAY, 7, 50))
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        plan = coordinator.data["kid_a"].plan
        assert plan.recommended_leave == _local(TUESDAY, 7, 46)
        assert plan.latest_leave == _local(TUESDAY, 7, 51)

    leave_now = [event for event in events if event.data["kind"] == "leave_now"]
    assert len(leave_now) == 1
    assert "leave_now" in _script_kinds(calls)


async def test_go_now_missed_while_down_is_not_sent_late(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A start 3 min after the departure time does not resend a missed go-now.

    Spec §11.3 only resurrects a missed go-now within 2 min, even though the
    trip (latest 08:00) is still reachable at 07:58.
    """
    events = async_capture_events(hass, EVENT_NOTIFICATION)
    async_mock_service(hass, "script", "turn_on")
    freezer.move_to(_local(TUESDAY, 7, 58))
    entry = _make_entry({"kid_a": _profile_dict("kid_a")}, **{OPT_DRY_RUN: False})
    schedules = {"kid_a": _FakeSchedule({TUESDAY: "Lektion MA"})}
    with _patch_factories(schedules):
        await _setup(hass, entry)
        coordinator = entry.runtime_data.coordinator
        await _advance(hass, freezer, _local(TUESDAY, 7, 59))
        freezer.move_to(_local(TUESDAY, 7, 59))
        await coordinator.async_refresh()
        await _advance(hass, freezer, _local(TUESDAY, 8, 0))

    assert "leave_now" not in _kinds(events)


# ---------------------------------------------------------------------------
# Bug 4: a profile failing its first refresh still gets entities
# ---------------------------------------------------------------------------


async def test_profile_failing_first_refresh_gets_entities_and_recovers(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A crashing profile shows unavailable entities that recover in place."""
    freezer.move_to(_local(TUESDAY, 6, 30))
    entry = _make_entry(
        {"kid_a": _profile_dict("kid_a"), "kid_b": _profile_dict("kid_b")}
    )
    schedules = {
        "kid_a": _FakeSchedule({TUESDAY: "Lektion MA"}),
        "kid_b": _FakeSchedule({TUESDAY: "Lektion MA"}),
    }
    schedules["kid_b"].fail = True
    with _patch_factories(schedules):
        await _setup(hass, entry)

        kid_b_status = _entity_id(hass, entry, "kid_b_departure_status")
        state = hass.states.get(kid_b_status)
        assert state is not None
        assert state.state == STATE_UNAVAILABLE
        kid_a_state = hass.states.get(_entity_id(hass, entry, "kid_a_departure_status"))
        assert kid_a_state is not None
        assert kid_a_state.state != STATE_UNAVAILABLE

        schedules["kid_b"].fail = False
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()

        state = hass.states.get(kid_b_status)
        assert state is not None
        assert state.state == "scheduled"
        assert entry.state is ConfigEntryState.LOADED
