"""Notification scheduler for the Family Departures integration (spec §11.3).

The scheduler is the timing half of the notification path: the pure
:func:`notification_policy.evaluate` decides *which* notices a mission needs for
a given ``now`` and ``MissionState``, and this module decides *when* to call it
and makes sure every decision is recorded in the Store before the dispatcher
(T18) is asked to deliver it.

What it does (spec §11.1 timing, §11.3 timers/restart):

* For every published :class:`DeparturePlan` it computes the mission's trigger
  points — the −60 min morning
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
  applies and no stale "go now" is sent (spec §11.3). The same stale filter
  (:func:`_drop_stale`) runs on every path, so a setup late in the day never
  sends a "morning" or "go now" for a mission that is already over.
* Tomorrow's preview (published by the coordinator from 18:00) is not a set of
  missions to notify but the input of one *day-before digest* per profile, a
  ``person:d:day`` mission with its own 20:00 point timer and ledger (plan
  §6.2). Tomorrow's missions send nothing else until their own day.
* A new coordinator round is evaluated right away, not only on the next tick,
  so a round that moves the departure earlier past ``now`` still gets its
  "go now" out before ``latest_leave``.
* The previous round's plan is kept per mission and passed to ``evaluate`` so
  an earlier departure can alert once it is stable over two rounds (§12.1).
* ``async_shutdown`` cancels every timer and the local tick so unload/reload
  leaves nothing behind (spec §11.3).

This module touches ``homeassistant`` (timers, callbacks) so it is deliberately
*not* pure; all planning math stays in the pure modules it calls.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from homeassistant.core import HassJob, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .coordinator import FamilyDeparturesCoordinator
from .models import (
    DeparturePlan,
    LunchMenu,
    MissionState,
    NotificationIntent,
    NotifiedRecord,
    PackingList,
    ProfileConfig,
)
from .notification_policy import (
    DIGEST_SLOT,
    EVENING_HOUR,
    KIND_CLEANUP,
    KIND_LEAVE_NOW,
    KIND_MORNING,
    MORNING_LEAD_MINUTES,
    REMINDER_LEAD_MINUTES,
    evaluate,
    evaluate_day_digest,
    evaluate_removed,
)
from .packing import build_packing_list
from .store import FamilyDeparturesStore
from .timeutil import TZ, make_mission_id

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
        # The plan published in the latest round and the one before it, per
        # mission. ``evaluate`` needs the previous round's plan to tell a
        # stable improvement from a one-off blip (spec §12.1). ``plan.revision``
        # is the config revision and does not change per round, so rounds are
        # told apart by plan object identity instead. After a restart one more
        # round is needed before an improvement can alert.
        self._last_plan: dict[str, DeparturePlan] = {}
        self._previous_plan: dict[str, DeparturePlan] = {}

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
        self._note_round(self._current_plans())
        await self._async_catch_up(now)
        self._rearm_all(now)
        # ``cancel_on_shutdown`` lets HA drop the tick on shutdown like its own
        # timers; unload still cancels it explicitly in ``async_shutdown``.
        self._cancel_tick = async_track_time_interval(
            self._hass,
            self._async_local_tick,
            LOCAL_TICK_INTERVAL,
            name="family_departures notification tick",
            cancel_on_shutdown=True,
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
        self._last_plan.clear()
        self._previous_plan.clear()

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
        plans = self._current_plans()
        self._note_round(plans)
        now = dt_util.utcnow()
        self._rearm_all(now)
        removed = self._removed_missions(plans, now)
        if removed:
            self._hass.async_create_task(
                self._async_notify_removed(removed, now),
                "family_departures removed missions",
                eager_start=False,
            )
        # Evaluate the new round right away instead of waiting up to 60 s for
        # the tick: a round that moves the departure earlier past ``now`` may
        # leave only a minute or two before ``latest_leave`` (spec §11.3).
        self._hass.async_create_task(
            self._async_evaluate_all(now),
            "family_departures evaluate new round",
            eager_start=False,
        )

    async def _async_evaluate_all(self, now: datetime) -> None:
        """Evaluate every current mission once (ledger keeps this idempotent)."""
        if self._shutdown:
            return
        for mission_id in list(self._current_plans()):
            await self._async_evaluate_mission(mission_id, now)
        for digest_id in list(self._current_digests()):
            await self._async_evaluate_digest(digest_id, now)

    async def _async_evaluate_digest(self, digest_id: str, now: datetime) -> None:
        """Run the day-before digest policy and record+dispatch what is due."""
        if self._shutdown:
            return
        digest = self._current_digests().get(digest_id)
        if digest is None:
            return
        due = self._digest_intents(digest, now)
        if due is None:
            return
        state, intents = due
        self._record_digest(digest, state, intents, now)
        await self._dispatch(intents)

    def _note_round(self, plans: dict[str, DeparturePlan]) -> None:
        """Shift last → previous for every mission whose plan object changed."""
        for mission_id, plan in plans.items():
            last = self._last_plan.get(mission_id)
            if plan is last:
                continue
            if last is not None:
                self._previous_plan[mission_id] = last
            self._last_plan[mission_id] = plan
        for mission_id in list(self._last_plan):
            if mission_id not in plans:
                self._last_plan.pop(mission_id)
                self._previous_plan.pop(mission_id, None)

    # -- Timer arming -------------------------------------------------------

    def _current_plans(self) -> dict[str, DeparturePlan]:
        """Every mission of today, by mission id.

        Tomorrow's preview is not included: it only feeds the day-before digest
        (:meth:`_current_digests`); its missions notify on their own day.
        """
        data = self._coordinator.data or {}
        plans: dict[str, DeparturePlan] = {}
        for result in data.values():
            if result.missions:
                for mission in result.missions:
                    plans[mission.plan.mission_id] = mission.plan
            elif result.plan is not None:
                plans[result.plan.mission_id] = result.plan
        return plans

    def _removed_missions(
        self, plans: dict[str, DeparturePlan], now: datetime
    ) -> list[tuple[DeparturePlan, MissionState, ProfileConfig]]:
        """Today's extra missions that left the calendar after a leave notice.

        Derived from the Store, so it survives a restart: a stored extra
        mission of today that is no longer published, whose profile was
        processed this round, whose link is still configured and did not fail
        to fetch, whose event is gone from the link's *unfiltered* calendar day
        (not just from the profile's filtered view), and whose person is not
        off or sick (that closes missions
        rather than removing events). :func:`evaluate_removed` then decides
        whether anyone was actually told about it.
        """
        data = self._coordinator.data or {}
        today = now.astimezone(TZ).date()
        removed: list[tuple[DeparturePlan, MissionState, ProfileConfig]] = []
        for mission_id, state in list(self._store.data.missions.items()):
            if mission_id in plans or ":x-" not in mission_id:
                continue
            stored = self._store.get_plan(mission_id)
            if stored is None or stored.requirement.local_date != today:
                continue
            req = stored.requirement
            result = data.get(req.person_id)
            if result is None or result.local_date != today:
                continue
            profile = result.profile
            if req.link_id is None or req.link_id in result.failed_extra_links:
                continue
            link = next(
                (lk for lk in profile.calendar_links if lk.link_id == req.link_id),
                None,
            )
            if link is None:
                continue
            # Still in the calendar but out of the profile's view (a link filter
            # was edited, an attendee alias is not resolved yet): not removed.
            raw_day = self._store.get_schedule(link.calendar_id, today)
            if raw_day is not None and any(
                event.uid == req.event_id for event in raw_day.events
            ):
                continue
            override = self._store.get_override(req.person_id, today)
            if override is not None and override.attendance in ("off", "sick"):
                continue
            removed.append((stored, state, profile))
        return removed

    async def _async_notify_removed(
        self,
        removed: list[tuple[DeparturePlan, MissionState, ProfileConfig]],
        now: datetime,
    ) -> None:
        """Send the one-off ``removed`` + ``cleanup`` pair per vanished mission."""
        if self._shutdown:
            return
        for plan, state, profile in removed:
            # Re-read: the ledger may have changed while the task was queued.
            current = self._store.get_mission(plan.mission_id) or state
            intents = evaluate_removed(plan, current, profile, now)
            if not intents:
                continue
            self._record_sent(current, intents, now)
            await self._dispatch(intents)

    def _current_digests(self) -> dict[str, _Digest]:
        """One day-before digest per profile whose tomorrow has missions."""
        data = self._coordinator.data or {}
        digests: dict[str, _Digest] = {}
        for result in data.values():
            plans = tuple(m.plan for m in result.tomorrow_missions)
            if not plans:
                continue
            day = plans[0].requirement.local_date
            mission_id = make_mission_id(result.profile.id, day, DIGEST_SLOT)
            packing = result.tomorrow_packing or build_packing_list(
                result.profile.id, day, (), (), None, ()
            )
            digests[mission_id] = _Digest(
                lunch=result.lunch_tomorrow,
                mission_id=mission_id,
                profile=result.profile,
                day=day,
                plans=plans,
                packing=packing,
            )
        return digests

    def _digest_intents(
        self, digest: _Digest, now: datetime
    ) -> tuple[MissionState, list[NotificationIntent]] | None:
        """The digest's due intents with its ledger state, or ``None``."""
        state = self._store.get_mission(digest.mission_id)
        if state is None:
            return None
        legacy = self._store.get_mission(make_mission_id(digest.profile.id, digest.day))
        intents = evaluate_day_digest(
            digest.profile,
            digest.day,
            digest.plans,
            digest.packing,
            digest.lunch,
            state,
            legacy,
            now,
        )
        return (state, intents) if intents else None

    def _record_digest(
        self,
        digest: _Digest,
        state: MissionState,
        intents: list[NotificationIntent],
        now: datetime,
    ) -> None:
        """Ledger the digest, and note it on each mission it told about.

        The per-mission record keeps change alerts comparing against the leave
        time the person was actually told in the digest, as the evening notice
        always did, rather than against the first preview.
        """
        self._record_sent(state, intents, now)
        kind = intents[0].kind
        for plan in digest.plans:
            mission = self._store.get_mission(plan.mission_id)
            if mission is None or kind in mission.notified:
                continue
            notified = {
                **mission.notified,
                kind: NotifiedRecord(
                    kind=kind,
                    sent_at=now,
                    leave_time=plan.recommended_leave,
                    revision=plan.revision,
                ),
            }
            self._store.set_mission(replace(mission, notified=notified))

    def _profiles(self) -> dict[str, ProfileConfig]:
        """Profile config keyed by person id, from the published round."""
        data = self._coordinator.data or {}
        return {result.profile.id: result.profile for result in data.values()}

    def _rearm_all(self, now: datetime) -> None:
        """(Re)register timers for every current plan; drop vanished missions."""
        plans = self._current_plans()
        digests = self._current_digests()
        # Cancel timers for missions and digests that are no longer published.
        for mission_id in list(self._timers):
            if mission_id not in plans and mission_id not in digests:
                self._cancel_mission_timers(mission_id)
        for mission_id, plan in plans.items():
            if self._armed_revision.get(mission_id) == plan.revision:
                continue
            self._arm_mission(plan, now)
        for digest_id, digest in digests.items():
            # The 20:00 wake-up does not depend on the plans: arm it once.
            if digest_id not in self._timers:
                self._arm_digest(digest, now)

    def _arm_digest(self, digest: _Digest, now: datetime) -> None:
        """Register the digest's 20:00 point timer on the day before."""
        when = _digest_time(digest.day)
        cancels: list[Callable[[], None]] = []
        if when > now:
            cancels.append(
                async_track_point_in_utc_time(
                    self._hass,
                    HassJob(
                        self._make_digest_callback(digest.mission_id),
                        "family_departures digest timer",
                        cancel_on_shutdown=True,
                    ),
                    when,
                )
            )
        self._timers[digest.mission_id] = cancels

    def _make_digest_callback(
        self, digest_id: str
    ) -> Callable[[datetime], Coroutine[object, object, None]]:
        async def _point(fire_time: datetime) -> None:
            await self._async_evaluate_digest(digest_id, dt_util.utcnow())

        return _point

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
                    HassJob(
                        self._make_point_callback(plan.mission_id),
                        "family_departures mission timer",
                        cancel_on_shutdown=True,
                    ),
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
        await self._async_evaluate_all(now)

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
            previous=self._previous_plan.get(mission_id),
            current=plan,
            state=state,
            packing=self._packing_for(plan),
            profile=profile,
            now=now,
        )
        intents = _drop_stale(
            intents,
            plan,
            now,
            catch_up=False,
            previous=self._previous_plan.get(mission_id),
        )
        if not intents:
            return

        # Record the ledger before dispatch so dedup survives a crash between
        # sending and journalling (spec §11.3).
        self._record_sent(state, intents, now)
        await self._dispatch(intents)

    def _packing_for(self, plan: DeparturePlan) -> PackingList:
        """Build the still-pending packing list for a plan's mission.

        The packing list is derived from the published schedule and the stored
        acknowledgements so the evening/morning/reminder notices can list what
        is left to pack (spec §5.5). Falls back to an empty list when the round
        has no schedule for the mission's source.
        """
        req = plan.requirement
        profile = self._profiles().get(req.person_id)
        if profile is None:
            return build_packing_list(req.person_id, req.local_date, (), (), None, ())
        # Read the profile's day across its calendar links from the shared cache
        # (plan §4.1), replacing the old per-profile destination_id read.
        from .day_events import profile_day_events

        events = profile_day_events(
            self._store,
            profile,
            req.local_date,
            self._coordinator.attendee_aliases(profile),
        )
        override = self._store.get_override(req.person_id, req.local_date)
        acknowledged = self._store.get_packing_acks(req.person_id, req.local_date)
        return build_packing_list(
            req.person_id,
            req.local_date,
            events,
            profile.packing_rules,
            override,
            acknowledged,
        )

    def _record_sent(
        self,
        state: MissionState,
        intents: list[NotificationIntent],
        now: datetime,
    ) -> None:
        """Write a :class:`NotifiedRecord` per intent into the ledger (spec §11.3)."""
        notified = dict(state.notified)
        for intent in intents:
            notified[intent.kind] = NotifiedRecord(
                kind=intent.kind,
                sent_at=now,
                leave_time=intent.recommended_leave_time,
                revision=intent.revision,
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
                previous=self._previous_plan.get(mission_id),
                current=plan,
                state=state,
                packing=self._packing_for(plan),
                profile=profile,
                now=now,
            )
            intents = _drop_stale(intents, plan, now, catch_up=True)
            if not intents:
                continue
            self._record_sent(state, intents, now)
            await self._dispatch(intents)

        for digest in self._current_digests().values():
            due = self._digest_intents(digest, now)
            if due is None:
                continue
            state, intents = due
            self._record_digest(digest, state, intents, now)
            await self._dispatch(intents)


# ---------------------------------------------------------------------------
# Stale-notice filter (pure)
# ---------------------------------------------------------------------------


def _drop_stale(
    intents: list[NotificationIntent],
    plan: DeparturePlan,
    now: datetime,
    *,
    catch_up: bool,
    previous: DeparturePlan | None = None,
) -> list[NotificationIntent]:
    """Drop notices that are no longer useful at ``now`` (spec §10, §11.3).

    Applied on every path (point timer, local tick and restart catch-up) so a
    setup or restart late in the day never sends a stale "morning" or "go now"
    for a mission whose departure has long passed:

    * a mission for a later day gets nothing: its preliminary plan follows
      evening traffic, so change and critical alerts wait for the trip's own
      day (spec §5.5, §12.1); the day-before digest has its own path;
    * past the mission timeout (``event_start + 60 min``) only ``cleanup`` is
      still sent;
    * ``morning`` is dropped once the recommended departure has passed;
    * ``leave_now`` is kept within :data:`CATCH_UP_LEAVE_NOW_WINDOW` after the
      recommended departure. On restart catch-up it also requires the trip to
      be feasible; otherwise the policy's late/disruption path covers it;
    * on the live path ``leave_now`` is also kept past that window when this
      round moved the departure earlier past ``now`` while the trip is still on
      time (``now <= latest_leave``): e.g. at 07:50 a round moves 07:55 → 07:46
      with latest 07:51. A "go now" missed during downtime is not resurrected
      this way, because then the departure did not move past ``now`` in a
      round the scheduler saw.

    In normal operation nothing is dropped: the point timers fire exactly at
    the thresholds and the tick within 60 s of them.
    """
    if plan.requirement.local_date > now.astimezone(TZ).date():
        return []

    timeout = plan.requirement.event_start + timedelta(minutes=MISSION_TIMEOUT_MINUTES)
    rec = plan.recommended_leave
    kept: list[NotificationIntent] = []
    for intent in intents:
        if intent.kind == KIND_CLEANUP:
            kept.append(intent)
            continue
        if now > timeout:
            continue
        if intent.kind == KIND_MORNING and rec is not None and now >= rec:
            continue
        if intent.kind == KIND_LEAVE_NOW:
            if rec is None:
                continue
            if catch_up:
                if now - rec > CATCH_UP_LEAVE_NOW_WINDOW or not plan.feasible:
                    continue
            elif now - rec > CATCH_UP_LEAVE_NOW_WINDOW and not _moved_past_now(
                previous, plan, now
            ):
                continue
        kept.append(intent)
    return kept


def _moved_past_now(
    previous: DeparturePlan | None, plan: DeparturePlan, now: datetime
) -> bool:
    """True if this round moved the departure earlier past ``now`` while on time.

    The previous round's departure was not yet stale (within the go-now window
    of ``now``), the new one is earlier, and the trip is still on time.
    """
    rec = plan.recommended_leave
    prev_rec = previous.recommended_leave if previous is not None else None
    if rec is None or prev_rec is None or plan.latest_leave is None:
        return False
    return (
        prev_rec > rec
        and now - prev_rec <= CATCH_UP_LEAVE_NOW_WINDOW
        and now <= plan.latest_leave
    )


# ---------------------------------------------------------------------------
# Trigger-time computation (pure)
# ---------------------------------------------------------------------------


def _trigger_times(plan: DeparturePlan) -> list[datetime]:
    """Return a mission's wake-up times in UTC (spec §10, §11.1).

    The morning, reminder and leave-now wake-ups are relative to the recommended
    departure; the hard timeout fires 60 min after the event starts and lets the
    local tick evaluate the mission one last time (closing it, spec §10). Static
    or unplanned missions with no recommended departure still get the timeout
    wake-up. The 20:00 day-before wake-up belongs to the digest, not to a
    mission (:func:`_digest_time`).
    """
    times: list[datetime] = []
    req = plan.requirement

    rec = plan.recommended_leave
    if rec is not None:
        times.append(rec - timedelta(minutes=MORNING_LEAD_MINUTES))
        times.append(rec - timedelta(minutes=REMINDER_LEAD_MINUTES))
        times.append(rec)

    times.append(req.event_start + timedelta(minutes=MISSION_TIMEOUT_MINUTES))
    return times


def _digest_time(day: date) -> datetime:
    """20:00 local on the day before ``day``, in UTC (plan §6.2)."""
    evening = datetime.combine(
        day - timedelta(days=1),
        datetime.min.time().replace(hour=EVENING_HOUR),
        tzinfo=TZ,
    )
    return evening.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _Digest:
    """One profile's day-before digest input, from tomorrow's preview."""

    mission_id: str
    profile: ProfileConfig
    day: date
    plans: tuple[DeparturePlan, ...]
    packing: PackingList
    lunch: LunchMenu | None = None


__all__ = [
    "CATCH_UP_LEAVE_NOW_WINDOW",
    "LOCAL_TICK_INTERVAL",
    "DispatchCallback",
    "NotificationScheduler",
]
