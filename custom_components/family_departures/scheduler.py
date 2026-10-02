"""Notification scheduler for the Family Departures integration (spec §11.3).

The scheduler is the timing half of the notification path: the pure
:func:`notification_policy.evaluate` decides *which* notices a mission needs for
a given ``now`` and ``MissionState``, and this module decides *when* to call it
and makes sure every decision is recorded in the Store before the dispatcher
(T18) is asked to deliver it.

What it does (spec §11.1 timing, §11.3 timers/restart):

* For every published :class:`DeparturePlan` it computes the mission's trigger
  points — the evening notice at 20:00 the day before, the −60 min morning
  notice, the −10 min reminder, the recommended-leave "go now" and the hard
  timeout at ``event_start + 60 min`` — and registers an
  ``async_track_point_in_utc_time`` wake-up for each future one (spec §10, §11.1).
* On a new plan *revision* for a mission it cancels that mission's old timers
  and registers fresh ones, so a replan never leaves a stale wake-up behind
  (spec §11.3).
* Every wake-up (and a 60-second local safety tick that needs no network)
  re-reads the current plan and :class:`MissionState`, checks the plan is still
  the one that armed the timer, runs ``evaluate`` and records a
  :class:`NotifiedRecord` for each intent in the Store *before* handing the
  intents to the dispatch callback, so a crash after sending cannot cause a
  duplicate on the next tick (spec §11.3 ``(mission_id, kind)`` dedup).
* On start it performs restart catch-up: it recomputes against the stored plans
  and only re-sends a missed "go now" within two minutes if the trip is still
  reachable; otherwise the policy's own ``cannot_arrive_on_time``/change path
  applies and no stale "go now" is sent (spec §11.3).
* ``async_shutdown`` cancels every timer and the local tick so unload/reload
  leaves nothing behind (spec §11.3).

This module touches ``homeassistant`` (timers, callbacks) so it is deliberately
*not* pure; all planning math stays in the pure modules it calls.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .coordinator import FamilyDeparturesCoordinator
from .models import (
    DeparturePlan,
    MissionState,
    NotificationIntent,
    NotifiedRecord,
    PackingList,
    ProfileConfig,
)
from .notification_policy import (
    EVENING_HOUR,
    MORNING_LEAD_MINUTES,
    REMINDER_LEAD_MINUTES,
    evaluate,
)
from .packing import build_packing_list
from .store import FamilyDeparturesStore
from .timeutil import TZ

_LOGGER = logging.getLogger(__name__)

# A missed "go now" is only resurrected within this window after the restart
# recompute, and only if the trip is still reachable (spec §11.3).
CATCH_UP_LEAVE_NOW_WINDOW = timedelta(minutes=2)

# The local safety tick that catches thresholds missed between point timers,
# without any network traffic (spec §11.3: a 60 s local check).
LOCAL_TICK_INTERVAL = timedelta(seconds=60)

# A mission's timers stop at the latest 60 min after the event starts (spec §10).
MISSION_TIMEOUT_MINUTES = 60

# The dispatch callback the scheduler hands intents to. Supplying it keeps the
# dispatcher (T18) decoupled: the scheduler owns timing and the ledger, the
# dispatcher owns delivery.
DispatchCallback = Callable[[list[NotificationIntent]], Awaitable[None]]


class NotificationScheduler:
    """Owns notification timers and the per-mission notification ledger."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: FamilyDeparturesCoordinator,
        store: FamilyDeparturesStore,
        dispatch: DispatchCallback,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._store = store
        self._dispatch = dispatch
        # Per-mission cancel callbacks for registered point timers, keyed by
        # mission id, plus the revision the timers were armed for.
        self._timers: dict[str, list[Callable[[], None]]] = {}
        self._armed_revision: dict[str, int] = {}
        self._cancel_tick: Callable[[], None] | None = None
        self._cancel_listener: Callable[[], None] | None = None
        self._shutdown = False

    # -- Lifecycle ----------------------------------------------------------

    async def async_start(self) -> None:
        """Begin scheduling: catch up, arm timers and start the local tick.

        Called once after the coordinator's first refresh. It subscribes to
        coordinator updates (so each successful round re-arms timers), runs the
        restart catch-up against whatever is already published, and starts the
        60-second local safety tick (spec §11.3).
        """
        self._cancel_listener = self._coordinator.async_add_listener(
            self._handle_coordinator_update
        )
        now = dt_util.utcnow()
        await self._async_catch_up(now)
        self._rearm_all(now)
        self._cancel_tick = async_track_time_interval(
            self._hass, self._async_local_tick, LOCAL_TICK_INTERVAL
        )

    async def async_shutdown(self) -> None:
        """Cancel every timer, the tick and the listener (spec §11.3).

        After shutdown the scheduler ignores further coordinator updates so a
        reload cannot leave duplicate listeners or lingering wake-ups behind.
        """
        self._shutdown = True
        if self._cancel_listener is not None:
            self._cancel_listener()
            self._cancel_listener = None
        if self._cancel_tick is not None:
            self._cancel_tick()
            self._cancel_tick = None
        self._cancel_all_timers()

    # -- Coordinator hook ---------------------------------------------------

    @callback
    def _handle_coordinator_update(self) -> None:
        """Re-arm timers whenever the coordinator publishes a new round.

        A plan whose revision changed since its timers were armed has its old
        wake-ups cancelled and new ones registered (spec §11.3). Missions that
        are no longer published lose their timers.
        """
        if self._shutdown:
            return
        self._rearm_all(dt_util.utcnow())

    # -- Timer arming -------------------------------------------------------

    def _current_plans(self) -> dict[str, DeparturePlan]:
        """The plans published for today, keyed by mission id."""
        data = self._coordinator.data or {}
        plans: dict[str, DeparturePlan] = {}
        for result in data.values():
            if result.plan is not None:
                plans[result.plan.mission_id] = result.plan
        return plans

    def _profiles(self) -> dict[str, ProfileConfig]:
        """Profile config keyed by person id, from the published round."""
        data = self._coordinator.data or {}
        return {result.profile.id: result.profile for result in data.values()}

    def _rearm_all(self, now: datetime) -> None:
        """(Re)register timers for every current plan; drop vanished missions."""
        plans = self._current_plans()
        # Cancel timers for missions that are no longer published.
        for mission_id in list(self._timers):
            if mission_id not in plans:
                self._cancel_mission_timers(mission_id)
        for mission_id, plan in plans.items():
            if self._armed_revision.get(mission_id) == plan.revision:
                continue
            self._arm_mission(plan, now)

    def _arm_mission(self, plan: DeparturePlan, now: datetime) -> None:
        """Register the mission's future trigger times (spec §10, §11.1)."""
        self._cancel_mission_timers(plan.mission_id)
        cancels: list[Callable[[], None]] = []
        for when in _trigger_times(plan):
            if when <= now:
                continue
            cancels.append(
                async_track_point_in_utc_time(
                    self._hass,
                    self._make_point_callback(plan.mission_id),
                    when,
                )
            )
        self._timers[plan.mission_id] = cancels
        self._armed_revision[plan.mission_id] = plan.revision

    def _make_point_callback(
        self, mission_id: str
    ) -> Callable[[datetime], Coroutine[object, object, None]]:
        """Build the wake-up callback for one mission's point timers."""

        async def _point(fire_time: datetime) -> None:
            await self._async_evaluate_mission(mission_id, dt_util.utcnow())

        return _point

    def _cancel_mission_timers(self, mission_id: str) -> None:
        """Cancel and forget all point timers for one mission."""
        for cancel in self._timers.pop(mission_id, []):
            cancel()
        self._armed_revision.pop(mission_id, None)

    def _cancel_all_timers(self) -> None:
        for mission_id in list(self._timers):
            self._cancel_mission_timers(mission_id)

    # -- Local safety tick --------------------------------------------------

    async def _async_local_tick(self, now: datetime) -> None:
        """Evaluate every active mission once a minute (no network, spec §11.3).

        This catches a threshold crossed between point timers (or just after a
        restart) and relies on the ledger to stay idempotent, so it never
        duplicates a notice an exact point timer already sent.
        """
        if self._shutdown:
            return
        for mission_id in list(self._current_plans()):
            await self._async_evaluate_mission(mission_id, now)

    # -- Evaluation + ledger ------------------------------------------------

    async def _async_evaluate_mission(self, mission_id: str, now: datetime) -> None:
        """Run the policy for one mission and record+dispatch its intents.

        The callback re-reads the live plan and :class:`MissionState` (spec
        §11.3): a timer armed for an older revision is ignored, and a mission
        that has since vanished does nothing. Each produced intent is written to
        the ledger *before* dispatch returns so a crash cannot resend it.
        """
        if self._shutdown:
            return
        plan = self._current_plans().get(mission_id)
        if plan is None:
            return
        profile = self._profiles().get(plan.requirement.person_id)
        if profile is None:
            return
        state = self._store.get_mission(mission_id)
        if state is None:
            return

        intents = evaluate(
            previous=None,
            current=plan,
            state=state,
            packing=self._packing_for(plan),
            profile=profile,
            now=now,
        )
        if not intents:
            return

        # Record the ledger before dispatch so dedup survives a crash between
        # sending and journalling (spec §11.3).
        self._record_sent(state, intents, plan, now)
        await self._dispatch(intents)

    def _packing_for(self, plan: DeparturePlan) -> PackingList:
        """Build the still-pending packing list for a plan's mission.

        The packing list is derived from the published schedule and the stored
        acknowledgements so the evening/morning/reminder notices can list what
        is left to pack (spec §5.5). Falls back to an empty list when the round
        has no schedule for the mission's source.
        """
        req = plan.requirement
        schedule = self._store.get_schedule(req.destination_id, req.local_date)
        events = schedule.events if schedule is not None else ()
        profile = self._profiles().get(req.person_id)
        rules = profile.packing_rules if profile is not None else ()
        override = self._store.get_override(req.person_id, req.local_date)
        acknowledged = self._store.get_packing_acks(req.person_id, req.local_date)
        return build_packing_list(
            req.person_id, req.local_date, events, rules, override, acknowledged
        )

    def _record_sent(
        self,
        state: MissionState,
        intents: list[NotificationIntent],
        plan: DeparturePlan,
        now: datetime,
    ) -> None:
        """Write a :class:`NotifiedRecord` per intent into the ledger (spec §11.3)."""
        notified = dict(state.notified)
        for intent in intents:
            notified[intent.kind] = NotifiedRecord(
                kind=intent.kind,
                sent_at=now,
                leave_time=intent.recommended_leave_time,
                revision=plan.revision,
            )
        self._store.set_mission(replace(state, notified=notified))

    # -- Restart catch-up ---------------------------------------------------

    async def _async_catch_up(self, now: datetime) -> None:
        """Re-send only still-valid missed notices after a restart (spec §11.3).

        For each published plan the policy is evaluated against the current
        ``now``. The ledger already suppresses anything previously sent, so this
        naturally re-emits only the notices whose threshold has been crossed and
        not yet recorded. A missed ``leave_now`` is additionally gated: it is
        only allowed if ``now`` is still within :data:`CATCH_UP_LEAVE_NOW_WINDOW`
        of the recommended departure and the trip is still feasible; otherwise it
        is dropped here and the policy's late/disruption handling covers it.
        """
        for mission_id, plan in self._current_plans().items():
            profile = self._profiles().get(plan.requirement.person_id)
            if profile is None:
                continue
            state = self._store.get_mission(mission_id)
            if state is None:
                continue

            intents = evaluate(
                previous=None,
                current=plan,
                state=state,
                packing=self._packing_for(plan),
                profile=profile,
                now=now,
            )
            intents = [
                intent for intent in intents if self._catch_up_allows(intent, plan, now)
            ]
            if not intents:
                continue
            self._record_sent(state, intents, plan, now)
            await self._dispatch(intents)

    def _catch_up_allows(
        self, intent: NotificationIntent, plan: DeparturePlan, now: datetime
    ) -> bool:
        """Whether a catch-up intent may still be sent after a restart.

        Only ``leave_now`` is gated: a stale "go now" is suppressed unless the
        recommended departure is within the catch-up window and the plan is
        still feasible (spec §11.3). Every other kind is left to the ledger.
        """
        if intent.kind != "leave_now":
            return True
        if plan.recommended_leave is None or not plan.feasible:
            return False
        return now - plan.recommended_leave <= CATCH_UP_LEAVE_NOW_WINDOW


# ---------------------------------------------------------------------------
# Trigger-time computation (pure)
# ---------------------------------------------------------------------------


def _trigger_times(plan: DeparturePlan) -> list[datetime]:
    """Return a mission's wake-up times in UTC (spec §10, §11.1).

    The evening notice fires at 20:00 local on the day before the trip; the
    morning, reminder and leave-now wake-ups are relative to the recommended
    departure; the hard timeout fires 60 min after the event starts and lets the
    local tick evaluate the mission one last time (closing it, spec §10). Static
    or unplanned missions with no recommended departure still get the evening
    and timeout wake-ups.
    """
    times: list[datetime] = []
    req = plan.requirement
    evening_local = datetime.combine(
        req.local_date - timedelta(days=1),
        datetime.min.time().replace(hour=EVENING_HOUR),
        tzinfo=TZ,
    )
    times.append(evening_local.astimezone(UTC))

    rec = plan.recommended_leave
    if rec is not None:
        times.append(rec - timedelta(minutes=MORNING_LEAD_MINUTES))
        times.append(rec - timedelta(minutes=REMINDER_LEAD_MINUTES))
        times.append(rec)

    times.append(req.event_start + timedelta(minutes=MISSION_TIMEOUT_MINUTES))
    return times


__all__ = [
    "CATCH_UP_LEAVE_NOW_WINDOW",
    "LOCAL_TICK_INTERVAL",
    "DispatchCallback",
    "NotificationScheduler",
]
