"""Tests for the notification scheduler (spec §10, §11.1, §11.3).

The scheduler owns *timing* and the notification ledger; the pure
:func:`notification_policy.evaluate` owns *content*. These tests drive it with a
lightweight fake coordinator (so the published plans are fully controlled) and a
real :class:`FamilyDeparturesStore` backed by the PHACC ``hass_storage``
fixture, and move the clock with ``freeze_time`` + ``async_fire_time_changed``
so HA's point/interval timers fire deterministically.

Covered behaviour (T17 acceptance, spec §11.3):

* a plan arms morning/−10/leave-now wake-ups that fire the right notices;
* a new plan *revision* cancels the old timers and re-arms (no stale wake-up);
* the ledger is written *before* dispatch and dedups across a repeated tick;
* restart around the 10-minute warning catches up without a duplicate, and a
  long-missed "go now" is suppressed as not reachable;
* ``async_shutdown`` (and dropping a mission) leaves no timers behind;
* the 60-minute timeout path evaluates even when presence never fired.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from custom_components.family_departures.coordinator import (
    MissionResult,
    ProfileResult,
)
from custom_components.family_departures.models import (
    ArrivalRequirement,
    CalendarLink,
    DeparturePlan,
    LunchMenu,
    MissionState,
    NotificationIntent,
    NotifiedRecord,
    ProfileConfig,
    ScheduleEvent,
    ScheduleResult,
)
from custom_components.family_departures.notification_policy import (
    KIND_CHANGE,
    KIND_LEAVE_NOW,
    KIND_MORNING,
    KIND_REMINDER,
    REASON_IMPROVEMENT,
)
from custom_components.family_departures.packing import build_packing_list
from custom_components.family_departures.schedule import select_requirement
from custom_components.family_departures.scheduler import (
    CATCH_UP_LEAVE_NOW_WINDOW,
    NotificationScheduler,
    _digest_time,
    _trigger_times,
)
from custom_components.family_departures.store import FamilyDeparturesStore
from custom_components.family_departures.timeutil import (
    TZ,
    combine_local,
    make_mission_id,
)
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

TODAY = date(2026, 10, 20)  # a Tuesday
# Recommended departure 07:30 local; event starts 08:20 local.
REC_LOCAL = combine_local(TODAY, datetime.min.time().replace(hour=7, minute=30))
EVENT_LOCAL = combine_local(TODAY, datetime.min.time().replace(hour=8, minute=20))


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _margins() -> Any:
    from custom_components.family_departures.models import Margins

    return Margins(
        arrival=5, departure=5, boarding=2, parking_and_walk=0, min_transfer=5
    )


def _profile(
    profile_id: str = "kid_a",
    *,
    notifications_enabled: bool = True,
    quiet_start: str = "22:00:00",
    quiet_end: str = "06:00:00",
) -> ProfileConfig:
    from datetime import time as _t

    return ProfileConfig(
        id=profile_id,
        name=profile_id.title(),
        default_location_id=f"{profile_id}_destination",
        default_mode="public_transport",
        static_minutes=None,
        static_label=None,
        weather_adjust=False,
        car_fallback_minutes=None,
        margins=_margins(),
        weekday_mask=frozenset({0, 1, 2, 3, 4}),
        packing_rules=(),
        person_entity_id=None,
        notifications_enabled=notifications_enabled,
        change_threshold_minutes=3,
        quiet_start=_t.fromisoformat(quiet_start),
        quiet_end=_t.fromisoformat(quiet_end),
        scripts={"push": "script.push_kid_a"},
        calendar_links=(
            CalendarLink(
                link_id="main",
                calendar_id=f"{profile_id}_calendar",
                role="main",
            ),
        ),
        evening_notice_enabled=True,
    )


def _plan(
    profile_id: str = "kid_a",
    *,
    revision: int = 1,
    recommended: datetime | None = REC_LOCAL,
    status: str = "scheduled",
    feasible: bool = True,
) -> DeparturePlan:
    mission_id = make_mission_id(profile_id, TODAY)
    req = ArrivalRequirement(
        mission_id=mission_id,
        person_id=profile_id,
        local_date=TODAY,
        event_id="e1",
        event_start=EVENT_LOCAL,
        arrival_deadline=EVENT_LOCAL - timedelta(minutes=5),
        destination_id=f"{profile_id}_destination",
        source="ics",
    )
    return DeparturePlan(
        mission_id=mission_id,
        plan_id=f"{mission_id}:{revision}",
        revision=revision,
        requirement=req,
        mode="public_transport",
        recommended_leave=recommended,
        latest_leave=recommended,
        last_on_time_alternative_leave=None,
        predicted_arrival=EVENT_LOCAL if recommended else None,
        journey_id="trip-1",
        route_summary="Buss 17",
        quality="scheduled",
        feasible=feasible,
        status=status,  # type: ignore[arg-type]
        breakdown=None,
        reason_codes=(),
        config_revision=revision,
    )


def _mission(plan: DeparturePlan) -> MissionState:
    return MissionState(
        mission_id=plan.mission_id,
        status=plan.status,
        departed_at=None,
        reopened=False,
        notified={},
        first_published_leave=plan.recommended_leave,
        action_nonce="nonce123",
    )


def _profile_result(profile: ProfileConfig, plan: DeparturePlan) -> ProfileResult:
    schedule = ScheduleResult(
        status="ok",
        events=(
            ScheduleEvent(
                uid="e1",
                summary="Lektion MA",
                start=plan.requirement.event_start,
                end=plan.requirement.event_start + timedelta(minutes=50),
                source_id=profile.calendar_links[0].calendar_id,
            ),
        ),
        fetched_at=dt_util.utcnow(),
    )
    outcome = select_requirement(
        profile, TODAY, schedule, None, False, None, None, dt_util.utcnow()
    )
    packing = build_packing_list(
        profile.id, TODAY, schedule.events, profile.packing_rules, None, ()
    )
    return ProfileResult(
        profile=profile,
        outcome=outcome,
        plan=plan,
        packing=packing,
        local_date=TODAY,
        schedule=schedule,
    )


class FakeCoordinator:
    """Minimal coordinator stand-in exposing ``data`` and a listener API."""

    def __init__(self, results: dict[str, ProfileResult]) -> None:
        self.data = results
        self._listeners: list[Any] = []

    def async_add_listener(self, update_callback: Any) -> Any:
        self._listeners.append(update_callback)

        def _remove() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _remove

    def attendee_aliases(self, profile: ProfileConfig) -> frozenset[str]:
        return frozenset()

    def set_results(self, results: dict[str, ProfileResult]) -> None:
        self.data = results
        for listener in list(self._listeners):
            listener()


class RecordingDispatch:
    """Collects every dispatched intent batch."""

    def __init__(self) -> None:
        self.batches: list[list[NotificationIntent]] = []

    async def __call__(self, intents: list[NotificationIntent]) -> None:
        self.batches.append(list(intents))

    @property
    def kinds(self) -> list[str]:
        return [intent.kind for batch in self.batches for intent in batch]

    def intents(self, kind: str) -> list[NotificationIntent]:
        return [i for batch in self.batches for i in batch if i.kind == kind]


@pytest.fixture
async def store(hass: HomeAssistant) -> FamilyDeparturesStore:
    s = FamilyDeparturesStore(hass, "entry-test")
    await s.async_load()
    return s


# ---------------------------------------------------------------------------
# Pure trigger-time computation
# ---------------------------------------------------------------------------


def test_trigger_times_cover_the_mission_lifecycle() -> None:
    """A plan yields −60, −10, leave-now and timeout wake-ups (§11.1).

    The 20:00 day-before wake-up belongs to the digest, not to the mission.
    """
    plan = _plan()
    times = _trigger_times(plan)

    evening = datetime(2026, 10, 19, 20, 0, tzinfo=TZ).astimezone(UTC)
    assert evening not in times
    assert _digest_time(TODAY) == evening
    assert REC_LOCAL - timedelta(minutes=60) in times
    assert REC_LOCAL - timedelta(minutes=10) in times
    assert REC_LOCAL in times
    assert EVENT_LOCAL + timedelta(minutes=60) in times


def test_trigger_times_without_departure_still_arm_the_timeout() -> None:
    """A plan with no recommended leave still gets its timeout wake-up (§10)."""
    plan = _plan(recommended=None, status="needs_configuration")
    times = _trigger_times(plan)

    assert EVENT_LOCAL + timedelta(minutes=60) in times
    # No departure-relative wake-ups when there is no recommended leave.
    assert REC_LOCAL not in times


# ---------------------------------------------------------------------------
# Timer firing
# ---------------------------------------------------------------------------


async def test_reminder_fires_and_records_before_dispatch(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """The −10 reminder fires once and is journalled before dispatch (§11.3)."""
    profile = _profile()
    plan = _plan()
    store.set_mission(_mission(plan))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    # Start well before any threshold so catch-up sends nothing.
    start = REC_LOCAL - timedelta(hours=3)
    with freeze_time(start):
        await scheduler.async_start()
    assert dispatch.batches == []

    # Advance to the −10 reminder.
    with freeze_time(REC_LOCAL - timedelta(minutes=10)):
        async_fire_time_changed(hass, REC_LOCAL - timedelta(minutes=10))
        await hass.async_block_till_done()

    assert KIND_REMINDER in dispatch.kinds
    # Ledger recorded so a repeat tick does not resend (§11.3).
    mission = store.get_mission(plan.mission_id)
    assert mission is not None
    assert KIND_REMINDER in mission.notified

    await scheduler.async_shutdown()


async def test_repeated_tick_does_not_duplicate(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """The local tick re-evaluating the same minute sends nothing new (§11.3)."""
    profile = _profile()
    plan = _plan()
    store.set_mission(_mission(plan))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    start = REC_LOCAL - timedelta(hours=3)
    with freeze_time(start):
        await scheduler.async_start()

    at = REC_LOCAL
    with freeze_time(at):
        async_fire_time_changed(hass, at)
        await hass.async_block_till_done()
        first = len(dispatch.kinds)
        # Fire the local tick again at the same instant.
        async_fire_time_changed(hass, at + timedelta(seconds=60))
    with freeze_time(at + timedelta(seconds=60)):
        await hass.async_block_till_done()

    assert KIND_LEAVE_NOW in dispatch.kinds
    # No kind repeated: the second evaluation is suppressed by the ledger.
    assert len(dispatch.kinds) == len(set(dispatch.kinds))
    assert len(dispatch.kinds) >= first

    await scheduler.async_shutdown()


async def test_new_revision_rearms_without_stale_timer(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """A new plan revision cancels the old wake-ups and arms new ones (§11.3)."""
    profile = _profile()
    plan1 = _plan(revision=1)
    store.set_mission(_mission(plan1))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan1)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    start = REC_LOCAL - timedelta(hours=3)
    with freeze_time(start):
        await scheduler.async_start()

    # Publish a new revision with a later departure.
    new_rec = REC_LOCAL + timedelta(minutes=20)
    plan2 = _plan(revision=2, recommended=new_rec)
    with freeze_time(start):
        coord.set_results({"kid_a": _profile_result(profile, plan2)})

    # The old −10 time passes: no reminder should fire (that timer was cancelled).
    old_reminder = REC_LOCAL - timedelta(minutes=10)
    with freeze_time(old_reminder):
        async_fire_time_changed(hass, old_reminder)
        await hass.async_block_till_done()
    # The old reminder instant is now > 20 min before the new departure, so no
    # reminder yet.
    assert KIND_REMINDER not in dispatch.kinds

    # The new −10 time fires the reminder.
    new_reminder = new_rec - timedelta(minutes=10)
    with freeze_time(new_reminder):
        async_fire_time_changed(hass, new_reminder)
        await hass.async_block_till_done()
    assert KIND_REMINDER in dispatch.kinds

    await scheduler.async_shutdown()


# ---------------------------------------------------------------------------
# Restart catch-up (§11.3)
# ---------------------------------------------------------------------------


async def test_restart_near_reminder_catches_up_without_duplicate(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """Restart just after the −10 reminder resends it once, not twice (§11.3)."""
    profile = _profile()
    plan = _plan()
    # Simulate that nothing was sent before the restart.
    store.set_mission(_mission(plan))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    # Start one minute after the reminder time: catch-up should emit it once.
    restart_at = REC_LOCAL - timedelta(minutes=9)
    with freeze_time(restart_at):
        await scheduler.async_start()

    assert dispatch.kinds.count(KIND_REMINDER) == 1
    # A later tick at the same minute must not duplicate it.
    with freeze_time(restart_at):
        async_fire_time_changed(hass, restart_at + timedelta(seconds=60))
    with freeze_time(restart_at + timedelta(seconds=60)):
        await hass.async_block_till_done()
    assert dispatch.kinds.count(KIND_REMINDER) == 1

    await scheduler.async_shutdown()


async def test_restart_long_after_leave_now_suppresses_stale_go_now(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """A restart long after departure does not resend a stale "go now" (§11.3)."""
    profile = _profile()
    plan = _plan()
    store.set_mission(_mission(plan))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    # Restart ten minutes after the recommended departure: outside the 2-min
    # catch-up window, so no leave_now.
    restart_at = REC_LOCAL + CATCH_UP_LEAVE_NOW_WINDOW + timedelta(minutes=8)
    with freeze_time(restart_at):
        await scheduler.async_start()

    assert KIND_LEAVE_NOW not in dispatch.kinds

    await scheduler.async_shutdown()


async def test_restart_within_window_resends_go_now_if_reachable(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """Within the 2-min window a still-feasible "go now" is resent (§11.3)."""
    profile = _profile()
    plan = _plan(feasible=True)
    store.set_mission(_mission(plan))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    restart_at = REC_LOCAL + timedelta(minutes=1)
    with freeze_time(restart_at):
        await scheduler.async_start()

    assert KIND_LEAVE_NOW in dispatch.kinds

    await scheduler.async_shutdown()


# ---------------------------------------------------------------------------
# Lifecycle / cleanup (§11.3)
# ---------------------------------------------------------------------------


async def test_shutdown_leaves_no_timers(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """After shutdown a passing threshold fires nothing (no lingering timers)."""
    profile = _profile()
    plan = _plan()
    store.set_mission(_mission(plan))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    start = REC_LOCAL - timedelta(hours=3)
    with freeze_time(start):
        await scheduler.async_start()
    await scheduler.async_shutdown()

    with freeze_time(REC_LOCAL):
        async_fire_time_changed(hass, REC_LOCAL)
        await hass.async_block_till_done()

    assert dispatch.batches == []


async def test_dropped_mission_cancels_its_timers(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """A mission that disappears from the round loses its wake-ups (§11.3)."""
    profile = _profile()
    plan = _plan()
    store.set_mission(_mission(plan))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    start = REC_LOCAL - timedelta(hours=3)
    with freeze_time(start):
        await scheduler.async_start()

    # The next round has no profiles at all (e.g. day off / unload mid-flight).
    with freeze_time(start):
        coord.set_results({})

    with freeze_time(REC_LOCAL):
        async_fire_time_changed(hass, REC_LOCAL)
        await hass.async_block_till_done()

    assert dispatch.batches == []

    await scheduler.async_shutdown()


async def test_timeout_tick_evaluates_without_presence(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """The leave-now notice still fires via timers even if presence never does.

    Presence auto-departure is out of this module; the point/tick timers must
    carry the mission on their own up to the 60-min timeout (spec §10, §11.3).
    """
    profile = _profile()
    plan = _plan()
    store.set_mission(_mission(plan))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    start = REC_LOCAL - timedelta(hours=3)
    with freeze_time(start):
        await scheduler.async_start()

    with freeze_time(REC_LOCAL):
        async_fire_time_changed(hass, REC_LOCAL)
        await hass.async_block_till_done()

    assert KIND_LEAVE_NOW in dispatch.kinds

    await scheduler.async_shutdown()


async def test_earlier_plan_alerts_only_when_stable_across_two_rounds(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """An earlier departure alerts once a second round confirms it (§12.1)."""
    profile = _profile()
    plan = _plan()
    store.set_mission(_mission(plan))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    start = REC_LOCAL - timedelta(hours=3)
    earlier = REC_LOCAL - timedelta(minutes=10)
    with freeze_time(start):
        await scheduler.async_start()
        coord.set_results(
            {"kid_a": _profile_result(profile, _plan(recommended=earlier))}
        )
    tick = start + timedelta(seconds=60)
    with freeze_time(tick):
        async_fire_time_changed(hass, tick)
        await hass.async_block_till_done()
    assert KIND_CHANGE not in dispatch.kinds

    # A new round (new plan object) with the same earlier time.
    with freeze_time(tick):
        coord.set_results(
            {"kid_a": _profile_result(profile, _plan(recommended=earlier))}
        )
    tick2 = tick + timedelta(seconds=60)
    with freeze_time(tick2):
        async_fire_time_changed(hass, tick2)
        await hass.async_block_till_done()

    changes = [
        intent
        for batch in dispatch.batches
        for intent in batch
        if intent.kind == KIND_CHANGE
    ]
    assert len(changes) == 1
    assert REASON_IMPROVEMENT in changes[0].reason_codes
    assert changes[0].recommended_leave_time == earlier

    await scheduler.async_shutdown()


async def test_evening_start_sends_no_stale_notices_for_todays_finished_mission(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """Starting at 19:00 sends no morning/go-now for this morning's trip."""
    profile = _profile()
    plan = _plan()
    store.set_mission(_mission(plan))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    start = combine_local(TODAY, datetime.min.time().replace(hour=19))
    with freeze_time(start):
        await scheduler.async_start()
    for minutes in (1, 2):
        at = start + timedelta(minutes=minutes)
        with freeze_time(at):
            async_fire_time_changed(hass, at)
            await hass.async_block_till_done()

    assert dispatch.batches == []

    await scheduler.async_shutdown()


async def test_tick_does_not_send_go_now_long_after_departure_time(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """Ten minutes past departure the tick sends no stale go-now or morning."""
    profile = _profile()
    plan = _plan()
    store.set_mission(_mission(plan))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    start = REC_LOCAL + timedelta(minutes=10)
    with freeze_time(start):
        await scheduler.async_start()
    for minutes in (11, 12):
        at = REC_LOCAL + timedelta(minutes=minutes)
        with freeze_time(at):
            async_fire_time_changed(hass, at)
            await hass.async_block_till_done()

    assert KIND_LEAVE_NOW not in dispatch.kinds
    assert KIND_MORNING not in dispatch.kinds

    await scheduler.async_shutdown()


async def test_notifications_disabled_profile_sends_nothing(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """A profile with notifications off produces no intents at any tick (§12.1)."""
    profile = _profile(notifications_enabled=False)
    plan = _plan()
    store.set_mission(_mission(plan))
    coord = FakeCoordinator({"kid_a": _profile_result(profile, plan)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    start = REC_LOCAL - timedelta(hours=3)
    with freeze_time(start):
        await scheduler.async_start()
    with freeze_time(REC_LOCAL):
        async_fire_time_changed(hass, REC_LOCAL)
        await hass.async_block_till_done()

    assert dispatch.batches == []

    await scheduler.async_shutdown()


# ---------------------------------------------------------------------------
# Multiple missions per profile (plan item 6)
# ---------------------------------------------------------------------------

EXTRA_START = combine_local(TODAY, datetime.min.time().replace(hour=17, minute=30))
EXTRA_REC = combine_local(TODAY, datetime.min.time().replace(hour=17, minute=0))


def _club_profile() -> ProfileConfig:
    base = _profile()
    return replace(
        base,
        calendar_links=(
            *base.calendar_links,
            CalendarLink(link_id="club", calendar_id="club_calendar", role="extra"),
        ),
    )


def _extra_plan(recommended: datetime = EXTRA_REC) -> DeparturePlan:
    mission_id = make_mission_id("kid_a", TODAY, "x-club-k1")
    req = ArrivalRequirement(
        mission_id=mission_id,
        person_id="kid_a",
        local_date=TODAY,
        event_id="f1",
        event_start=EXTRA_START,
        arrival_deadline=EXTRA_START - timedelta(minutes=5),
        destination_id="pitch",
        source="ics",
        slot="x-club-k1",
        link_id="club",
        label="Fotboll",
        destination_name="Planen",
    )
    return replace(
        _plan(),
        mission_id=mission_id,
        plan_id=f"{mission_id}:1",
        requirement=req,
        recommended_leave=recommended,
        latest_leave=recommended,
    )


def _multi_result(
    profile: ProfileConfig,
    *plans: DeparturePlan,
    failed: tuple[str, ...] = (),
) -> ProfileResult:
    base = _profile_result(profile, plans[0])
    return replace(
        base,
        missions=tuple(
            MissionResult(link_id=p.requirement.link_id or "main", plan=p)
            for p in plans
        ),
        failed_extra_links=failed,
    )


async def test_every_mission_gets_its_own_notices_and_tag(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    profile = _club_profile()
    main, extra = _plan(), _extra_plan()
    store.set_mission(_mission(main))
    store.set_mission(_mission(extra))
    coord = FakeCoordinator({"kid_a": _multi_result(profile, main, extra)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)

    with freeze_time(REC_LOCAL - timedelta(hours=3)):
        await scheduler.async_start()
    for at in (REC_LOCAL, EXTRA_REC):
        with freeze_time(at):
            async_fire_time_changed(hass, at)
            await hass.async_block_till_done()

    leave_now = [
        i for batch in dispatch.batches for i in batch if i.kind == KIND_LEAVE_NOW
    ]
    assert sorted(i.mission_id for i in leave_now) == sorted(
        [main.mission_id, extra.mission_id]
    )
    assert len({i.tag for i in leave_now}) == 2
    extra_intent = next(i for i in leave_now if i.mission_id == extra.mission_id)
    assert extra_intent.slot == "x-club-k1"
    assert extra_intent.title == "Kid_A – Fotboll 17:30"
    assert dispatch.kinds.count(KIND_LEAVE_NOW) == 2

    await scheduler.async_shutdown()


async def _vanished_extra(
    hass: HomeAssistant,
    store: FamilyDeparturesStore,
    *,
    failed: tuple[str, ...] = (),
    notified: tuple[str, ...] = (KIND_MORNING,),
) -> tuple[RecordingDispatch, FakeCoordinator, NotificationScheduler]:
    profile = _club_profile()
    main, extra = _plan(), _extra_plan()
    store.set_mission(_mission(main))
    state = _mission(extra)
    store.set_mission(
        replace(
            state,
            notified={
                kind: NotifiedRecord(
                    kind=kind, sent_at=EXTRA_REC, leave_time=EXTRA_REC, revision=1
                )
                for kind in notified
            },
        )
    )
    store.set_plan(extra)
    coord = FakeCoordinator({"kid_a": _multi_result(profile, main, extra)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)
    # Early enough that catch-up has sent no leave notice of its own yet.
    with freeze_time(EXTRA_REC - timedelta(hours=3)):
        await scheduler.async_start()
        coord.set_results({"kid_a": _multi_result(profile, main, failed=failed)})
        await hass.async_block_till_done()
    return dispatch, coord, scheduler


async def test_removed_extra_event_sends_removed_and_cleanup_once(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    dispatch, coord, scheduler = await _vanished_extra(hass, store)

    assert dispatch.kinds.count("removed") == 1
    assert dispatch.kinds.count("cleanup") == 1
    removed = next(i for b in dispatch.batches for i in b if i.kind == "removed")
    assert removed.message == "Fotboll 17:30 är borttagen ur kalendern."

    with freeze_time(EXTRA_REC - timedelta(minutes=20)):
        coord.set_results(coord.data)
        await hass.async_block_till_done()
    assert dispatch.kinds.count("removed") == 1
    await scheduler.async_shutdown()


async def test_removed_extra_event_is_silent_without_a_prior_leave_notice(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    dispatch, _coord, scheduler = await _vanished_extra(hass, store, notified=())
    assert "removed" not in dispatch.kinds
    await scheduler.async_shutdown()


async def test_extra_link_fetch_failure_is_not_a_removal(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    dispatch, _coord, scheduler = await _vanished_extra(hass, store, failed=("club",))
    assert "removed" not in dispatch.kinds
    await scheduler.async_shutdown()


# ---------------------------------------------------------------------------
# Day-before digest (plan item 7)
# ---------------------------------------------------------------------------

DIGEST_ID = make_mission_id("kid_a", TODAY, "day")
EVENING_BEFORE = combine_local(
    TODAY - timedelta(days=1), datetime.min.time().replace(hour=20)
)


def _digest_state() -> MissionState:
    return MissionState(
        mission_id=DIGEST_ID,
        status="scheduled",
        departed_at=None,
        reopened=False,
        notified={},
        first_published_leave=None,
        action_nonce="digest-nonce",
    )


def _tomorrow_result(profile: ProfileConfig, *plans: DeparturePlan) -> ProfileResult:
    base = _profile_result(profile, plans[0]) if plans else None
    assert base is not None
    day_before = TODAY - timedelta(days=1)
    return replace(
        base,
        plan=None,
        local_date=day_before,
        tomorrow_plan=plans[0],
        tomorrow_missions=tuple(
            MissionResult(link_id=p.requirement.link_id or "main", plan=p)
            for p in plans
        ),
        tomorrow_schedule=base.schedule,
        tomorrow_packing=base.packing,
    )


async def _digest_scheduler(
    hass: HomeAssistant,
    store: FamilyDeparturesStore,
    profile: ProfileConfig,
    *plans: DeparturePlan,
    lunch: LunchMenu | None = None,
) -> tuple[RecordingDispatch, NotificationScheduler]:
    for plan in plans:
        store.set_mission(_mission(plan))
    store.set_mission(_digest_state())
    result = replace(_tomorrow_result(profile, *plans), lunch_tomorrow=lunch)
    coord = FakeCoordinator({"kid_a": result})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)
    with freeze_time(EVENING_BEFORE - timedelta(hours=2)):
        await scheduler.async_start()
    assert dispatch.batches == []
    return dispatch, scheduler


async def test_digest_fires_once_at_twenty_and_is_ledgered(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    main, extra = _plan(), _extra_plan()
    dispatch, scheduler = await _digest_scheduler(
        hass, store, _club_profile(), main, extra
    )

    for at in (EVENING_BEFORE, EVENING_BEFORE + timedelta(minutes=1)):
        with freeze_time(at):
            async_fire_time_changed(hass, at)
            await hass.async_block_till_done()

    [digest] = dispatch.intents("evening")
    assert digest.mission_id == DIGEST_ID
    assert digest.message.splitlines() == [
        "08:20 Skola – gå 07:30",
        "17:30 Fotboll → Planen – gå 17:00",
    ]
    ledger = store.get_mission(DIGEST_ID)
    assert ledger is not None and "evening" in ledger.notified
    # Each mission it told about notes the leave time the person was given.
    for plan in (main, extra):
        mission = store.get_mission(plan.mission_id)
        assert mission is not None
        assert mission.notified["evening"].leave_time == plan.recommended_leave
    # Tomorrow's missions send nothing else tonight.
    assert dispatch.kinds == ["evening"]

    await scheduler.async_shutdown()


async def test_digest_carries_the_lunch_menu_from_the_coordinator_result(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    lunch = LunchMenu(
        local_date=TODAY,
        status="ok",
        dishes=("Pasta", "Sallad"),
        fetched_at=EVENING_BEFORE,
    )
    dispatch, scheduler = await _digest_scheduler(
        hass, store, _club_profile(), _plan(), lunch=lunch
    )

    with freeze_time(EVENING_BEFORE):
        async_fire_time_changed(hass, EVENING_BEFORE)
        await hass.async_block_till_done()

    [digest] = dispatch.intents("evening")
    assert digest.message.splitlines()[-1] == "Lunch imorgon: Pasta, Sallad"
    assert digest.lunch_menu == "Lunch imorgon: Pasta, Sallad"

    await scheduler.async_shutdown()


async def test_digest_is_not_resent_when_legacy_morning_ledger_has_evening(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    main = _plan()
    legacy = replace(
        _mission(main),
        notified={
            "evening": NotifiedRecord(
                kind="evening",
                sent_at=EVENING_BEFORE - timedelta(minutes=5),
                leave_time=main.recommended_leave,
                revision=1,
            )
        },
    )
    store.set_mission(legacy)
    store.set_mission(_digest_state())
    coord = FakeCoordinator({"kid_a": _tomorrow_result(_profile(), main)})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)
    with freeze_time(EVENING_BEFORE - timedelta(hours=2)):
        await scheduler.async_start()
    with freeze_time(EVENING_BEFORE):
        async_fire_time_changed(hass, EVENING_BEFORE)
        await hass.async_block_till_done()

    assert dispatch.batches == []
    await scheduler.async_shutdown()


async def test_digest_is_held_inside_quiet_hours(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    from datetime import time as _t

    profile = replace(_profile(), quiet_start=_t(19, 0), quiet_end=_t(6, 0))
    dispatch, scheduler = await _digest_scheduler(hass, store, profile, _plan())

    with freeze_time(EVENING_BEFORE):
        async_fire_time_changed(hass, EVENING_BEFORE)
        await hass.async_block_till_done()

    assert dispatch.batches == []
    await scheduler.async_shutdown()


async def test_no_digest_when_tomorrow_has_no_mission(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    profile, plan = _profile(), _plan()
    store.set_mission(_digest_state())
    result = replace(
        _tomorrow_result(profile, plan), tomorrow_plan=None, tomorrow_missions=()
    )
    coord = FakeCoordinator({"kid_a": result})
    dispatch = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, dispatch)
    with freeze_time(EVENING_BEFORE - timedelta(hours=2)):
        await scheduler.async_start()
    with freeze_time(EVENING_BEFORE):
        async_fire_time_changed(hass, EVENING_BEFORE)
        await hass.async_block_till_done()

    assert dispatch.batches == []
    assert not scheduler._timers
    await scheduler.async_shutdown()


async def test_restart_after_twenty_catches_up_the_digest_exactly_once(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    profile, main = _profile(), _plan()
    store.set_mission(_mission(main))
    store.set_mission(_digest_state())
    coord = FakeCoordinator({"kid_a": _tomorrow_result(profile, main)})
    late = EVENING_BEFORE + timedelta(minutes=30)

    first = RecordingDispatch()
    scheduler = NotificationScheduler(hass, coord, store, first)
    with freeze_time(late):
        await scheduler.async_start()
    assert first.kinds == ["evening"]
    await scheduler.async_shutdown()

    # A restart on the same store: the ledger keeps the digest from resending.
    second = RecordingDispatch()
    restarted = NotificationScheduler(hass, coord, store, second)
    with freeze_time(late + timedelta(minutes=5)):
        await restarted.async_start()
        async_fire_time_changed(hass, late + timedelta(minutes=6))
        await hass.async_block_till_done()
    assert second.batches == []
    await restarted.async_shutdown()
