"""End-to-end: a school day with evening activities (plan item 6).

A real coordinator, store, scheduler and dispatcher run against fake providers
whose journeys follow the deadline they are asked for, so every mission gets a
plan of its own. The scenarios:

* a weekday with school plus two evening activities: each mission gets its own
  morning / reminder / leave-now notice exactly once with a distinct tag, and
  the published (active) mission moves from school to football once school is
  confirmed departed;
* a Saturday with one activity gets leave notices planned from home;
* an extra event removed from the calendar after its morning notice sends
  ``removed`` plus ``cleanup`` once;
* ``FD_SKIP`` skips only the tapped mission.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from custom_components.family_departures.const import (
    DATA_HOME_LAT,
    DATA_HOME_LON,
    DATA_HOUSEHOLD_NAME,
    DATA_ICS_URLS,
    DOMAIN,
    MOBILE_APP_ACTION_EVENT,
    OPT_PROFILES,
)
from custom_components.family_departures.coordinator import (
    FamilyDeparturesCoordinator,
    ProviderFactories,
)
from custom_components.family_departures.dispatcher import NotificationDispatcher
from custom_components.family_departures.models import (
    DurationResult,
    Journey,
    JourneyResult,
    Leg,
    NotificationIntent,
    ScheduleEvent,
    ScheduleResult,
)
from custom_components.family_departures.notification_policy import (
    KIND_CLEANUP,
    KIND_LEAVE_NOW,
    KIND_MORNING,
    KIND_REMINDER,
    KIND_REMOVED,
)
from custom_components.family_departures.scheduler import NotificationScheduler
from custom_components.family_departures.store import FamilyDeparturesStore
from custom_components.family_departures.timeutil import combine_local, make_mission_id
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

HOME = (59.33, 18.06)
SCHOOL_DEST = (59.4, 18.1)
PITCH_DEST = (59.5, 18.2)
WEEKDAY = date(2026, 10, 20)  # a Tuesday
SATURDAY = date(2026, 10, 24)


# ---------------------------------------------------------------------------
# Config: one kid, a main school link and an extra club link
# ---------------------------------------------------------------------------


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


def _entry() -> MockConfigEntry:
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
        # Monday to Friday: on Saturday the school link is silent.
        "weekday_mask": [0, 1, 2, 3, 4],
        "packing_rules": [],
        "person_entity_id": None,
        "notifications_enabled": True,
        "evening_notice_enabled": True,
        "change_threshold_minutes": 3,
        "quiet_start": "22:00:00",
        "quiet_end": "06:00:00",
        "scripts": {"push": "script.push_kid_a"},
        "calendar_links": [
            _link("school", "school_cal", "main"),
            _link("club", "club_cal", "extra", label="Fotboll", location_id="pitch"),
        ],
    }
    options = {
        OPT_PROFILES: {"kid_a": profile},
        "locations": {
            "school": {
                "id": "school",
                "name": "Skolan",
                "lat": SCHOOL_DEST[0],
                "lon": SCHOOL_DEST[1],
            },
            "pitch": {
                "id": "pitch",
                "name": "Planen",
                "lat": PITCH_DEST[0],
                "lon": PITCH_DEST[1],
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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSchedule:
    def __init__(self, *events: ScheduleEvent, day: date) -> None:
        self.day = day
        self.set_events(*events)

    def set_events(self, *events: ScheduleEvent) -> None:
        self.result = ScheduleResult(
            status="ok" if events else "empty",
            events=events,
            fetched_at=combine_local(self.day, time(5, 0)),
        )

    async def async_get_day(self, d: date) -> ScheduleResult:
        if d != self.day:
            return ScheduleResult(
                status="empty", events=(), fetched_at=self.result.fetched_at
            )
        return self.result


class DeadlineJourney:
    """One bus that arrives 15 minutes before whatever deadline is asked for."""

    def __init__(self) -> None:
        self.origins: list[tuple[float, float]] = []

    async def async_plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        deadline: datetime,
        earliest_departure: datetime | None,
    ) -> JourneyResult:
        self.origins.append(origin)
        arrival = deadline - timedelta(minutes=15)
        leg = Leg(
            kind="transit",
            line="17",
            from_stop="Hemma",
            to_stop="Målet",
            planned_departure=arrival - timedelta(minutes=25),
            planned_arrival=arrival,
        )
        return JourneyResult(
            status="ok",
            journeys=(Journey(journey_id=f"trip-{deadline:%H%M}", legs=(leg,)),),
            fetched_at=deadline - timedelta(hours=12),
        )


class FakeCar:
    async def async_get_duration(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        realtime: bool,
        time_delta: timedelta | None,
    ) -> DurationResult:
        return DurationResult(
            minutes=20.0,
            fetched_at=datetime(2026, 10, 20, tzinfo=UTC),
            source="waze",
            quality="realtime",
        )


class RecordingDispatch:
    def __init__(self) -> None:
        self.batches: list[list[NotificationIntent]] = []

    async def __call__(self, intents: list[NotificationIntent]) -> None:
        self.batches.append(list(intents))

    def intents(self, kind: str | None = None) -> list[NotificationIntent]:
        return [
            i for batch in self.batches for i in batch if kind is None or i.kind == kind
        ]

    def kinds_all(self) -> list[str]:
        return [i.kind for i in self.intents()]

    def for_mission(self, mission_id: str) -> list[str]:
        return [i.kind for i in self.intents() if i.mission_id == mission_id]


def _event(
    day: date,
    uid: str,
    hh: int,
    mm: int,
    *,
    summary: str = "Aktivitet",
    key: str | None = None,
) -> ScheduleEvent:
    start = combine_local(day, time(hh, mm))
    return ScheduleEvent(
        uid=uid,
        summary=summary,
        start=start,
        end=start + timedelta(hours=1),
        source_id="src",
        occurrence_key=key,
    )


class Harness:
    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: FamilyDeparturesCoordinator,
        store: FamilyDeparturesStore,
        scheduler: NotificationScheduler,
        dispatch: RecordingDispatch,
        journey: DeadlineJourney,
        school: FakeSchedule,
        club: FakeSchedule,
        day: date,
    ) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.store = store
        self.scheduler = scheduler
        self.dispatch = dispatch
        self.journey = journey
        self.school = school
        self.club = club
        self.day = day

    def mission_id(self, slot: str = "morning") -> str:
        return make_mission_id("kid_a", self.day, slot)

    def plan(self, slot: str = "morning"):
        plan = self.store.get_plan(self.mission_id(slot))
        assert plan is not None, slot
        assert plan.recommended_leave is not None
        return plan

    async def publish(self, at: datetime) -> None:
        with freeze_time(at):
            await self.coordinator.async_refresh()
            await self.hass.async_block_till_done()

    async def tick(self, at: datetime) -> None:
        with freeze_time(at):
            async_fire_time_changed(self.hass, at)
            await self.hass.async_block_till_done()


async def _build(
    hass: HomeAssistant,
    day: date,
    school_events: tuple[ScheduleEvent, ...],
    club_events: tuple[ScheduleEvent, ...],
    *,
    start_at: datetime | None = None,
) -> Harness:
    start_at = start_at or combine_local(day, time(5, 0))
    entry = _entry()
    entry.add_to_hass(hass)
    store = FamilyDeparturesStore(hass, entry.entry_id)
    await store.async_load(now=start_at)

    school = FakeSchedule(*school_events, day=day)
    club = FakeSchedule(*club_events, day=day)
    journey = DeadlineJourney()
    providers = {"school_cal": school, "club_cal": club}
    factories = ProviderFactories(
        schedule=lambda calendar, ics_url: providers[calendar.id],
        journey=lambda: journey,
        car=lambda: FakeCar(),
    )
    coordinator = FamilyDeparturesCoordinator(hass, entry, store, factories=factories)
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coordinator, store, dispatch)
    with freeze_time(start_at):
        await coordinator.async_refresh()
        await scheduler.async_start()
        await hass.async_block_till_done()
    return Harness(
        hass, coordinator, store, scheduler, dispatch, journey, school, club, day
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def test_school_and_two_activities_each_notify_once(hass: HomeAssistant) -> None:
    h = await _build(
        hass,
        WEEKDAY,
        (_event(WEEKDAY, "lesson", 8, 20, summary="Lektion"),),
        (
            _event(WEEKDAY, "f1", 17, 30, key="k1"),
            _event(WEEKDAY, "f2", 19, 30, key="k2"),
        ),
    )
    slots = ["morning", "x-club-k1", "x-club-k2"]
    plans = {slot: h.plan(slot) for slot in slots}
    # Each mission is planned against its own deadline: the leaves are ordered.
    leaves = [plans[slot].recommended_leave for slot in slots]
    assert leaves == sorted(leaves)
    # The school mission is the active one until it is done.
    assert h.coordinator.data["kid_a"].plan.mission_id == h.mission_id()

    for slot in slots:
        rec = plans[slot].recommended_leave
        await h.tick(rec - timedelta(minutes=60))
        await h.tick(rec - timedelta(minutes=10))
        await h.tick(rec)

    for slot in slots:
        kinds = h.dispatch.for_mission(h.mission_id(slot))
        for kind in (KIND_MORNING, KIND_REMINDER, KIND_LEAVE_NOW):
            assert kinds.count(kind) == 1, (slot, kind, kinds)
    tags = {i.tag for i in h.dispatch.intents(KIND_LEAVE_NOW)}
    assert tags == {f"departure_{h.mission_id(slot)}" for slot in slots}
    football = next(
        i
        for i in h.dispatch.intents(KIND_LEAVE_NOW)
        if i.mission_id == h.mission_id("x-club-k1")
    )
    assert football.title == "Kid A – Fotboll 17:30"

    await h.scheduler.async_shutdown()


async def test_active_mission_moves_to_football_after_school_departed(
    hass: HomeAssistant,
) -> None:
    h = await _build(
        hass,
        WEEKDAY,
        (_event(WEEKDAY, "lesson", 8, 20, summary="Lektion"),),
        (_event(WEEKDAY, "f1", 17, 30, key="k1"),),
    )
    state = h.store.get_mission(h.mission_id())
    assert state is not None
    rec = h.plan().recommended_leave
    h.store.set_mission(
        replace(state, status="departed", departed_at=rec, reopened=False)
    )

    await h.publish(rec + timedelta(minutes=5))

    result = h.coordinator.data["kid_a"]
    assert result.plan is not None
    assert result.plan.mission_id == h.mission_id("x-club-k1")
    assert result.outcome.requirement == result.plan.requirement
    assert len(result.missions) == 2

    await h.scheduler.async_shutdown()


async def test_saturday_activity_notifies_from_home(hass: HomeAssistant) -> None:
    h = await _build(
        hass,
        SATURDAY,
        (),
        (_event(SATURDAY, "match", 10, 0, key="k1"),),
    )
    result = h.coordinator.data["kid_a"]
    assert result.outcome.day_status == "has_event"
    assert [m.requirement.slot for m in result.missions] == ["x-club-k1"]
    assert set(h.journey.origins) == {HOME}

    rec = h.plan("x-club-k1").recommended_leave
    await h.tick(rec - timedelta(minutes=60))
    await h.tick(rec)
    kinds = h.dispatch.for_mission(h.mission_id("x-club-k1"))
    assert KIND_MORNING in kinds and KIND_LEAVE_NOW in kinds

    await h.scheduler.async_shutdown()


async def test_removed_event_after_morning_notice_sends_removed_once(
    hass: HomeAssistant,
) -> None:
    h = await _build(
        hass,
        WEEKDAY,
        (),
        (_event(WEEKDAY, "f1", 17, 30, summary="Träning", key="k1"),),
    )
    mission_id = h.mission_id("x-club-k1")
    rec = h.plan("x-club-k1").recommended_leave
    await h.tick(rec - timedelta(minutes=60))
    assert KIND_MORNING in h.dispatch.for_mission(mission_id)

    # The club cancels the practice; the next round no longer has the event.
    h.club.set_events()
    await h.publish(rec - timedelta(minutes=45))
    await h.publish(rec - timedelta(minutes=40))

    kinds = h.dispatch.for_mission(mission_id)
    assert kinds.count(KIND_REMOVED) == 1
    assert kinds.count(KIND_CLEANUP) == 1
    removed = h.dispatch.intents(KIND_REMOVED)[0]
    assert removed.message == "Fotboll 17:30 är borttagen ur kalendern."
    # The vanished mission arms nothing: no leave_now follows.
    await h.tick(rec)
    assert KIND_LEAVE_NOW not in h.dispatch.for_mission(mission_id)

    await h.scheduler.async_shutdown()


async def test_skip_action_skips_only_that_mission(hass: HomeAssistant) -> None:
    h = await _build(
        hass,
        WEEKDAY,
        (),
        (
            _event(WEEKDAY, "f1", 17, 30, key="k1"),
            _event(WEEKDAY, "f2", 19, 30, key="k2"),
        ),
    )
    dispatcher = NotificationDispatcher(
        hass,
        h.store,
        lambda: h.coordinator.profile_configs,
        dry_run=lambda: True,
        test_recipient=lambda: None,
        request_refresh=h.coordinator.async_request_refresh,
        aliases=h.coordinator.attendee_aliases,
    )
    dispatcher.async_start()
    first, second = h.mission_id("x-club-k1"), h.mission_id("x-club-k2")
    nonce = h.store.get_mission(first).action_nonce

    with freeze_time(combine_local(WEEKDAY, time(6, 0))):
        hass.bus.async_fire(
            MOBILE_APP_ACTION_EVENT, {"action": f"FD_SKIP|{first}|{nonce}"}
        )
        await hass.async_block_till_done()
    assert h.store.get_mission(first).status == "skipped"
    assert h.store.get_mission(second).status != "skipped"

    rec = h.plan("x-club-k2").recommended_leave
    await h.tick(rec)
    assert KIND_LEAVE_NOW in h.dispatch.for_mission(second)
    assert KIND_LEAVE_NOW not in h.dispatch.for_mission(first)

    dispatcher.async_shutdown()
    await h.scheduler.async_shutdown()


async def test_evening_before_sends_one_digest_listing_every_mission(
    hass: HomeAssistant,
) -> None:
    tuesday = WEEKDAY
    h = await _build(
        hass,
        tuesday,
        (_event(tuesday, "lesson", 8, 20, summary="Lektion"),),
        (
            _event(tuesday, "f1", 17, 30, key="k1"),
            _event(tuesday, "f2", 19, 30, key="k2"),
        ),
        start_at=combine_local(date(2026, 10, 19), time(19, 0)),
    )
    result = h.coordinator.data["kid_a"]
    assert len(result.tomorrow_missions) == 3
    digest_id = h.mission_id("day")
    assert h.store.get_mission(digest_id) is not None

    for at in (
        combine_local(date(2026, 10, 19), time(19, 30)),
        combine_local(date(2026, 10, 19), time(20, 0)),
        combine_local(date(2026, 10, 19), time(20, 1)),
    ):
        await h.tick(at)

    [digest] = h.dispatch.intents("evening")
    assert digest.mission_id == digest_id
    lines = digest.message.splitlines()
    assert [line[:5] for line in lines] == ["08:20", "17:30", "19:30"]
    assert lines[0].startswith("08:20 Skola – gå ")
    assert "Fotboll → Planen – gå " in lines[1]
    # Nothing else is sent for tomorrow's missions tonight.
    assert h.dispatch.kinds_all() == ["evening"]

    await h.scheduler.async_shutdown()
