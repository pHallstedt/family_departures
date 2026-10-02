"""End-to-end morning scenarios (T21, spec §16, §17 "Klart när" Steg 1-3).

These tie together the real pieces -- the :class:`FamilyDeparturesCoordinator`
(driven by *fake* SL/Waze/ICS providers), the real
:class:`FamilyDeparturesStore` on the PHACC ``hass_storage`` fixture and the
real :class:`NotificationScheduler` -- and run a whole morning on controlled
time. The dispatcher's *content* decisions come from the pure policy; here we
record the delivered intents through a dispatch callable and assert the
user-visible lifecycle (which notices fire, in which order, and that departure
and cleanup behave), not internal call sequences.

Covered §16 rows (see ``docs/test-coverage.md`` for the full mapping):

* a whole morning per profile: evening -> morning -> reminder -> leave_now ->
  departed -> cleanup, with no duplicates in normal operation;
* a cancelled first SL journey yields a reachable alternative and a change/
  disruption notice without moving the home departure unsafely later;
* a restart around the 10-minute warning catches up once, without a duplicate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any

from custom_components.family_departures.const import (
    DATA_HOME_LAT,
    DATA_HOME_LON,
    DATA_HOUSEHOLD_NAME,
    DATA_ICS_URLS,
    DOMAIN,
    OPT_PROFILES,
)
from custom_components.family_departures.coordinator import (
    FamilyDeparturesCoordinator,
    ProfileResult,
    ProviderFactories,
)
from custom_components.family_departures.models import (
    DurationResult,
    Journey,
    JourneyResult,
    Leg,
    NotificationIntent,
    ProfileConfig,
    ScheduleEvent,
    ScheduleResult,
)
from custom_components.family_departures.notification_policy import (
    KIND_CLEANUP,
    KIND_EVENING,
    KIND_LEAVE_NOW,
    KIND_MORNING,
    KIND_REMINDER,
)
from custom_components.family_departures.scheduler import NotificationScheduler
from custom_components.family_departures.store import FamilyDeparturesStore
from custom_components.family_departures.timeutil import (
    TZ,
    combine_local,
    make_mission_id,
)
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

HOME = (59.33, 18.06)
TODAY = date(2026, 10, 20)  # a Tuesday, inside the weekday mask
# A morning "now" whose local date is TODAY (CEST, UTC+2 before the Oct 25 DST
# change): the coordinator plans for its current local date.
MORNING = combine_local(TODAY, time(5, 0))
# First lesson 08:20 local; a transit journey departs 07:30 and arrives 08:05.
EVENT_LOCAL = combine_local(TODAY, time(8, 20))


# ---------------------------------------------------------------------------
# Stored profile fixture (same shape the options flow writes, T12)
# ---------------------------------------------------------------------------


def _profile_dict(
    profile_id: str,
    *,
    default_mode: str = "public_transport",
    static_minutes: int | None = None,
    car_fallback_minutes: int | None = None,
    packing_match: str | None = None,
) -> dict[str, Any]:
    rules = (
        [{"id": "r1", "match": packing_match, "item": "Gympakläder"}]
        if packing_match
        else []
    )
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
        "packing_rules": rules,
        "person_entity_id": None,
        "notifications_enabled": True,
        "evening_notice_enabled": True,
        "change_threshold_minutes": 3,
        "quiet_start": "22:00:00",
        "quiet_end": "06:00:00",
        "scripts": {"push": f"script.push_{profile_id}"},
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
            DATA_ICS_URLS: {pid: f"https://example.test/{pid}" for pid in profiles},
        },
        options={OPT_PROFILES: dict(profiles)},
    )


# ---------------------------------------------------------------------------
# Fake providers (mutable, so a scenario can change the schedule mid-morning)
# ---------------------------------------------------------------------------


class FakeSchedule:
    def __init__(self, result: ScheduleResult) -> None:
        self.result = result
        self.calls: list[date] = []

    async def async_get_day(self, d: date) -> ScheduleResult:
        self.calls.append(d)
        return self.result


class FakeJourney:
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


class RecordingDispatch:
    """Records every intent batch handed over for delivery."""

    def __init__(self) -> None:
        self.batches: list[list[NotificationIntent]] = []

    async def __call__(self, intents: list[NotificationIntent]) -> None:
        self.batches.append(list(intents))

    def kinds_for(self, person_id: str) -> list[str]:
        return [
            intent.kind
            for batch in self.batches
            for intent in batch
            if intent.person_id == person_id
        ]

    @property
    def all_kinds(self) -> list[str]:
        return [intent.kind for batch in self.batches for intent in batch]


class FakeCoordinator:
    """Minimal coordinator stand-in exposing ``data`` and the listener API.

    Used only by the evening-summary test, which needs a plan whose mission is
    the *next* day's -- something the real coordinator (which plans its current
    local date) does not publish in a single run.
    """

    def __init__(self, results: dict[str, ProfileResult]) -> None:
        self.data = results
        self._listeners: list[Any] = []

    def async_add_listener(self, update_callback: Any) -> Any:
        self._listeners.append(update_callback)

        def _remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _remove


def _lesson(uid: str, summary: str, hour: int, minute: int) -> ScheduleEvent:
    start = combine_local(TODAY, time(hour, minute))
    return ScheduleEvent(
        uid=uid,
        summary=summary,
        start=start,
        end=start + timedelta(minutes=50),
        source_id="src",
    )


def _schedule(*events: ScheduleEvent, now: datetime) -> ScheduleResult:
    return ScheduleResult(status="ok", events=events, fetched_at=now)


def _transit(dep_local: time, arr_local: time, *, cancelled: bool = False) -> Journey:
    return Journey(
        journey_id=f"trip-{dep_local.isoformat()}",
        legs=(
            Leg(
                kind="transit",
                line="17",
                direction="Centrum",
                from_stop="Hemma",
                to_stop="Skolan",
                planned_departure=combine_local(TODAY, dep_local),
                planned_arrival=combine_local(TODAY, arr_local),
                cancelled=cancelled,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Harness: real coordinator + real store + real scheduler + recording dispatch
# ---------------------------------------------------------------------------


class Harness:
    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        store: FamilyDeparturesStore,
        scheduler: NotificationScheduler,
        dispatch: RecordingDispatch,
    ) -> None:
        self.coordinator = coordinator
        self.store = store
        self.scheduler = scheduler
        self.dispatch = dispatch

    async def publish(self, hass: HomeAssistant, at: datetime) -> None:
        """Recompute plans at ``at`` and let the scheduler re-arm."""
        with freeze_time(at):
            await self.coordinator.async_refresh()
            await hass.async_block_till_done()

    async def tick(self, hass: HomeAssistant, at: datetime) -> None:
        """Fire HA's point/interval timers at ``at``."""
        with freeze_time(at):
            async_fire_time_changed(hass, at)
            await hass.async_block_till_done()


async def _build(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    schedules: Mapping[str, FakeSchedule],
    journey: FakeJourney,
    car: FakeCar,
    start_at: datetime,
) -> Harness:
    entry.add_to_hass(hass)
    store = FamilyDeparturesStore(hass, entry.entry_id)
    await store.async_load(now=start_at)

    def make_schedule(profile: ProfileConfig, ics_url: str | None) -> FakeSchedule:
        return schedules[profile.id]

    factories = ProviderFactories(
        schedule=make_schedule,
        journey=lambda: journey,
        car=lambda: car,
    )
    coordinator = FamilyDeparturesCoordinator(hass, entry, store, factories=factories)
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coordinator, store, dispatch)

    with freeze_time(start_at):
        await coordinator.async_refresh()
        await scheduler.async_start()
        await hass.async_block_till_done()
    return Harness(coordinator, store, scheduler, dispatch)


# ---------------------------------------------------------------------------
# Scenario 1: a whole morning per profile
# ---------------------------------------------------------------------------


async def test_full_morning_transit_lifecycle(hass: HomeAssistant) -> None:
    """Morning -> reminder -> leave_now -> departed -> cleanup, no duplicates.

    §16: "Två lektioner och en uppgift tidigt på morgonen" (first real lesson
    chosen) and the Steg 3 "hela meddelandelivscykeln" done criterion. The
    evening summary (fired at 20:00 the evening before the mission) is covered
    separately in :func:`test_evening_summary_and_packing_fire_the_night_before`
    because the coordinator plans for its *current* local date.
    """
    entry = _entry({"kid_a": _profile_dict("kid_a", packing_match="IDRO")})
    # Two lessons: the 08:20 lesson is the first of the day; the 10:00 one is
    # later. The earliest start wins (spec §5.3).
    sched = FakeSchedule(
        _schedule(
            _lesson("e1", "Lektion IDRO1000X", 8, 20),
            _lesson("e2", "Lektion MA", 10, 0),
            now=MORNING,
        )
    )
    journey = FakeJourney(
        JourneyResult(
            status="ok",
            journeys=(_transit(time(7, 30), time(8, 5)),),
            fetched_at=MORNING,
            has_realtime=True,
        )
    )
    car = FakeCar(
        DurationResult(
            minutes=20.0, fetched_at=MORNING, source="waze", quality="realtime"
        )
    )
    h = await _build(
        hass,
        entry,
        schedules={"kid_a": sched},
        journey=journey,
        car=car,
        start_at=MORNING,
    )

    mission_id = make_mission_id("kid_a", TODAY)
    plan = h.store.get_plan(mission_id)
    assert plan is not None
    assert plan.recommended_leave is not None
    rec = plan.recommended_leave
    # The first lesson (08:20) drives the plan, not the 10:00 one (spec §5.3).
    assert plan.requirement.event_start == EVENT_LOCAL
    # Packing from the PE lesson is on today's list (spec §5.5).
    assert "Gympakläder" in h.coordinator.data["kid_a"].packing.items

    # Morning (−60) and reminder (−10).
    await h.tick(hass, rec - timedelta(minutes=60))
    assert KIND_MORNING in h.dispatch.kinds_for("kid_a")
    await h.tick(hass, rec - timedelta(minutes=10))
    assert KIND_REMINDER in h.dispatch.kinds_for("kid_a")

    # Leave now.
    await h.tick(hass, rec)
    assert KIND_LEAVE_NOW in h.dispatch.kinds_for("kid_a")

    # The user confirms departure; a recompute must keep it closed (spec §10).
    state = h.store.get_mission(mission_id)
    assert state is not None
    with freeze_time(rec + timedelta(minutes=1)):
        h.store.set_mission(
            replace(
                state,
                status="departed",
                departed_at=rec + timedelta(minutes=1),
                reopened=False,
            )
        )
    await h.publish(hass, rec + timedelta(minutes=2))

    # Cleanup fires at the timeout; no leave repeats afterwards.
    await h.tick(hass, EVENT_LOCAL + timedelta(minutes=60))
    kinds = h.dispatch.kinds_for("kid_a")
    assert KIND_CLEANUP in kinds
    # No duplicate leave_now in normal operation (Steg 3 done criterion).
    assert kinds.count(KIND_LEAVE_NOW) == 1

    await h.scheduler.async_shutdown()
    # Unload leaves no timers: a later tick dispatches nothing new (spec §16).
    before = len(h.dispatch.batches)
    await h.tick(hass, EVENT_LOCAL + timedelta(minutes=90))
    assert len(h.dispatch.batches) == before


# ---------------------------------------------------------------------------
# Scenario 2: cancelled first SL journey
# ---------------------------------------------------------------------------


async def test_cancelled_bus_morning_offers_reachable_alternative(
    hass: HomeAssistant,
) -> None:
    """§16: "SL-buss inställd" -> new reachable journey and a change notice.

    The first journey is cancelled; a later still-on-time journey is offered and
    the home departure is not pushed unsafely later than the on-time option.
    """
    evening_before = MORNING
    entry = _entry({"kid_b": _profile_dict("kid_b")})
    sched = FakeSchedule(
        _schedule(_lesson("e1", "Lektion MA", 8, 20), now=evening_before)
    )
    # Two candidates: the earlier 07:30 bus is cancelled, the 07:40 is valid.
    journey = FakeJourney(
        JourneyResult(
            status="ok",
            journeys=(
                _transit(time(7, 30), time(8, 5), cancelled=True),
                _transit(time(7, 40), time(8, 12)),
            ),
            fetched_at=evening_before,
            has_realtime=True,
        )
    )
    car = FakeCar(
        DurationResult(
            minutes=20.0, fetched_at=evening_before, source="waze", quality="realtime"
        )
    )
    h = await _build(
        hass,
        entry,
        schedules={"kid_b": sched},
        journey=journey,
        car=car,
        start_at=evening_before,
    )

    mission_id = make_mission_id("kid_b", TODAY)
    plan = h.store.get_plan(mission_id)
    assert plan is not None
    assert plan.feasible is True
    # The cancelled 07:30 journey is not selected; the valid one is.
    assert plan.journey_id == "trip-07:40:00"
    # Arrival is still on time (<= deadline), so no false "cannot arrive".
    assert plan.status != "cannot_arrive_on_time"

    await h.scheduler.async_shutdown()


async def test_no_journey_in_time_shows_late_without_false_on_time(
    hass: HomeAssistant,
) -> None:
    """§16: "Ingen resa kan ge ankomst i tid" -> late, no false latest-on-time."""
    evening_before = MORNING
    entry = _entry({"kid_b": _profile_dict("kid_b")})
    sched = FakeSchedule(
        _schedule(_lesson("e1", "Lektion MA", 8, 20), now=evening_before)
    )
    # The only journey arrives after the 08:15 deadline (08:20 − 5 min arrival).
    journey = FakeJourney(
        JourneyResult(
            status="ok",
            journeys=(_transit(time(8, 0), time(8, 40)),),
            fetched_at=evening_before,
            has_realtime=True,
        )
    )
    car = FakeCar(
        DurationResult(
            minutes=20.0, fetched_at=evening_before, source="waze", quality="realtime"
        )
    )
    h = await _build(
        hass,
        entry,
        schedules={"kid_b": sched},
        journey=journey,
        car=car,
        start_at=evening_before,
    )

    plan = h.store.get_plan(make_mission_id("kid_b", TODAY))
    assert plan is not None
    assert plan.status == "cannot_arrive_on_time"
    assert plan.feasible is False
    # No fabricated on-time alternative.
    assert plan.last_on_time_alternative_leave is None

    await h.scheduler.async_shutdown()


# ---------------------------------------------------------------------------
# Scenario 3: restart around the 10-minute warning
# ---------------------------------------------------------------------------


async def test_restart_near_reminder_catches_up_once(hass: HomeAssistant) -> None:
    """§16: "Omstart kring 10-minutersvarning" -> one catch-up, no duplicate.

    A fresh scheduler (as after an HA restart) starting just after the −10 mark,
    with nothing yet in the ledger, emits the reminder exactly once.
    """
    evening_before = MORNING
    entry = _entry({"parent_b": _profile_dict("parent_b")})
    sched = FakeSchedule(
        _schedule(_lesson("e1", "Lektion MA", 8, 20), now=evening_before)
    )
    journey = FakeJourney(
        JourneyResult(
            status="ok",
            journeys=(_transit(time(7, 30), time(8, 5)),),
            fetched_at=evening_before,
            has_realtime=True,
        )
    )
    car = FakeCar(
        DurationResult(
            minutes=20.0, fetched_at=evening_before, source="waze", quality="realtime"
        )
    )
    # Build once to compute and persist the plan, then shut the scheduler down
    # to model the pre-restart state (plan + mission in the Store).
    h = await _build(
        hass,
        entry,
        schedules={"parent_b": sched},
        journey=journey,
        car=car,
        start_at=evening_before,
    )
    await h.scheduler.async_shutdown()

    mission_id = make_mission_id("parent_b", TODAY)
    plan = h.store.get_plan(mission_id)
    assert plan is not None and plan.recommended_leave is not None
    rec = plan.recommended_leave

    # "Restart" one minute after the −10 reminder with a fresh scheduler.
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, h.coordinator, h.store, dispatch)
    restart_at = rec - timedelta(minutes=9)
    with freeze_time(restart_at):
        await h.coordinator.async_refresh()
        await scheduler.async_start()
        await hass.async_block_till_done()

    assert dispatch.kinds_for("parent_b").count(KIND_REMINDER) == 1
    # A later tick at the same minute does not duplicate it (ledger dedup).
    with freeze_time(restart_at):
        async_fire_time_changed(hass, restart_at + timedelta(seconds=60))
    with freeze_time(restart_at + timedelta(seconds=60)):
        await hass.async_block_till_done()
    assert dispatch.kinds_for("parent_b").count(KIND_REMINDER) == 1

    await scheduler.async_shutdown()


# ---------------------------------------------------------------------------
# Scenario 4: all four profiles plan together, mixed modes
# ---------------------------------------------------------------------------


async def test_all_four_profiles_get_explainable_plans(hass: HomeAssistant) -> None:
    """§16 acceptance: all four profiles configure and show explainable times."""
    morning = MORNING
    entry = _entry(
        {
            "kid_a": _profile_dict("kid_a", packing_match="IDRO"),
            "kid_b": _profile_dict("kid_b"),
            "parent_a": _profile_dict(
                "parent_a", default_mode="static", static_minutes=15
            ),
            "parent_b": _profile_dict(
                "parent_b", default_mode="car", car_fallback_minutes=18
            ),
        }
    )
    schedules = {
        "kid_a": FakeSchedule(
            _schedule(_lesson("e1", "Lektion IDRO1000X", 8, 20), now=morning)
        ),
        "kid_b": FakeSchedule(
            _schedule(_lesson("e1", "Lektion MA", 8, 20), now=morning)
        ),
        "parent_a": FakeSchedule(_schedule(_lesson("e1", "Arbete", 9, 0), now=morning)),
        "parent_b": FakeSchedule(
            _schedule(_lesson("e1", "Arbete", 8, 30), now=morning)
        ),
    }
    journey = FakeJourney(
        JourneyResult(
            status="ok",
            journeys=(_transit(time(7, 30), time(8, 5)),),
            fetched_at=morning,
            has_realtime=True,
        )
    )
    car = FakeCar(
        DurationResult(
            minutes=20.0, fetched_at=morning, source="waze", quality="realtime"
        )
    )
    h = await _build(
        hass,
        entry,
        schedules=schedules,
        journey=journey,
        car=car,
        start_at=morning,
    )

    data = h.coordinator.data
    assert set(data) == {"kid_a", "kid_b", "parent_a", "parent_b"}
    for pid in ("kid_a", "kid_b", "parent_a", "parent_b"):
        plan = data[pid].plan
        assert plan is not None, pid
        assert plan.recommended_leave is not None, pid
    # Modes are honoured per profile.
    assert data["kid_a"].plan.mode == "public_transport"
    assert data["parent_a"].plan.mode == "static"
    assert data["parent_b"].plan.mode == "car"
    # Kid A's PE lesson produced a packing item (spec §5.5).
    assert "Gympakläder" in data["kid_a"].packing.items

    await h.scheduler.async_shutdown()


# ---------------------------------------------------------------------------
# Scenario 5: evening summary and packing fire the night before
# ---------------------------------------------------------------------------


async def test_evening_summary_and_packing_fire_the_night_before(
    hass: HomeAssistant,
) -> None:
    """§16: "Idrott som tredje lektion" -> packing in the evening notice.

    The evening summary and its packing items are armed for 20:00 the evening
    before the mission's event. The real coordinator plans its current local
    date, so this drives the scheduler with a fake coordinator holding a plan
    for TODAY while the clock is the evening before (2026-10-19).
    """
    from custom_components.family_departures.models import (
        ArrivalRequirement,
        DeparturePlan,
        Margins,
        MissionState,
        PackingList,
        PackingRule,
        RequirementOutcome,
        SourceFilter,
    )

    profile = ProfileConfig(
        id="kid_a",
        name="Kid A",
        source_type="ics",
        calendar_entity_id=None,
        source_filter=SourceFilter(exclude_patterns=(), include_patterns=()),
        destination_id="kid_a_destination",
        dest_lat=59.4,
        dest_lon=18.1,
        default_mode="public_transport",
        static_minutes=None,
        static_label=None,
        weather_adjust=False,
        car_fallback_minutes=None,
        margins=Margins(
            arrival=5, departure=5, boarding=2, parking_and_walk=0, min_transfer=5
        ),
        weekday_mask=frozenset({0, 1, 2, 3, 4}),
        packing_rules=(PackingRule(id="r1", match="IDRO", item="Gympakläder"),),
        person_entity_id=None,
        notifications_enabled=True,
        change_threshold_minutes=3,
        quiet_start=time(22, 0),
        quiet_end=time(6, 0),
        scripts={"push": "script.push_kid_a"},
        evening_notice_enabled=True,
    )
    mission_id = make_mission_id("kid_a", TODAY)
    rec = combine_local(TODAY, time(7, 30))
    req = ArrivalRequirement(
        mission_id=mission_id,
        person_id="kid_a",
        local_date=TODAY,
        event_id="e1",
        event_start=EVENT_LOCAL,
        arrival_deadline=EVENT_LOCAL - timedelta(minutes=5),
        destination_id="kid_a_destination",
        source="ics",
    )
    plan = DeparturePlan(
        mission_id=mission_id,
        plan_id=f"{mission_id}:1",
        revision=1,
        requirement=req,
        mode="public_transport",
        recommended_leave=rec,
        latest_leave=rec,
        last_on_time_alternative_leave=None,
        predicted_arrival=EVENT_LOCAL,
        journey_id="trip-1",
        route_summary="Buss 17",
        quality="scheduled",
        feasible=True,
        status="scheduled",
        breakdown=None,
        reason_codes=(),
        config_revision=1,
    )
    outcome = RequirementOutcome(
        day_status="has_event", requirement=req, reason_codes=()
    )
    # A PE lesson on the schedule produces the "Gympakläder" packing item.
    packing = PackingList(
        person_id="kid_a",
        local_date=TODAY,
        items=("Gympakläder",),
        acknowledged=(),
    )
    result = ProfileResult(
        profile=profile,
        outcome=outcome,
        plan=plan,
        packing=packing,
        local_date=TODAY,
        schedule=ScheduleResult(status="ok", events=(), fetched_at=MORNING),
    )

    store = FamilyDeparturesStore(hass, "entry-evening")
    await store.async_load()
    # The scheduler derives packing from the stored schedule, so persist a PE
    # lesson for the mission's source/date (spec §5.5).
    store.set_schedule(
        "kid_a_destination",
        TODAY,
        _schedule(_lesson("e1", "Lektion IDRO1000X", 8, 20), now=MORNING),
    )
    store.set_mission(
        MissionState(
            mission_id=mission_id,
            status="scheduled",
            departed_at=None,
            reopened=False,
            notified={},
            first_published_leave=rec,
            action_nonce="nonce123",
        )
    )
    coord = FakeCoordinator({"kid_a": result})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    # Start at 18:00 the evening before, before the 20:00 evening trigger.
    evening_before_18 = datetime(2026, 10, 19, 18, 0, tzinfo=TZ)
    with freeze_time(evening_before_18):
        await scheduler.async_start()
    assert dispatch.all_kinds == []

    # The 20:00 evening trigger fires the evening summary with the packing item.
    evening_trigger = datetime(2026, 10, 19, 20, 0, tzinfo=TZ)
    with freeze_time(evening_trigger):
        async_fire_time_changed(hass, evening_trigger)
        await hass.async_block_till_done()

    kinds = dispatch.kinds_for("kid_a")
    assert KIND_EVENING in kinds
    evening_intents = [
        intent
        for batch in dispatch.batches
        for intent in batch
        if intent.kind == KIND_EVENING
    ]
    assert evening_intents
    assert "Gympakläder" in evening_intents[0].packing_items

    await scheduler.async_shutdown()
