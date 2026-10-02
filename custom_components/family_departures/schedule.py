"""First-event selection and day locking (spec §4.3, §5.3, §5.4).

:func:`select_requirement` turns a day's :class:`ScheduleResult` into a
:class:`RequirementOutcome`: either a concrete :class:`ArrivalRequirement` for
the morning mission, or a :class:`DayStatus` that tells the dashboard and the
notification policy why there is no mission (day off, no activity, nothing
registered, or a source error).

This module is pure (plan §1 rule 3): no ``homeassistant`` import, ``now`` is a
parameter. All datetimes are aware UTC; the local date ``d`` is already
computed in ``Europe/Stockholm`` by the caller.

Key rules (spec §5.3):

* The day's first start is chosen from the *whole* local day, not only future
  events, so a passed 08:20 does not turn 09:15 into a new morning trip.
* Before departure, a first event whose UID disappeared since the previous
  fetch (a possible SchoolSoft cancellation, §5.2) or that overlaps a cancelled
  marker is skipped so the start moves to the next valid lesson.
* After confirmed departure the morning mission is locked: ``off``/``sick``/
  ``remote`` overrides still close it, but ordinary schedule changes do not
  move the start. Only an explicit ``reopen_today`` (recorded as
  ``state.reopened``) lets a change take effect again.
* A fetch error, a successful empty schedule and an explicit day off are three
  distinct outcomes (§5.4); they are never collapsed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .models import (
    ArrivalRequirement,
    DayOverride,
    MissionState,
    ProfileConfig,
    RequirementOutcome,
    ScheduleEvent,
    ScheduleResult,
)
from .timeutil import combine_local, make_mission_id

# Reason codes attached to the outcome so the dashboard/diagnostics can explain
# why a day resolved the way it did without re-deriving the logic.
REASON_OVERRIDE_OFF = "override_off"
REASON_HOLIDAY = "holiday"
REASON_NOT_EXPECTED_DAY = "not_expected_day"
REASON_SOURCE_ERROR = "source_error"
REASON_STALE_CACHE = "stale_cache"
REASON_EMPTY_SCHEDULE = "empty_schedule"
REASON_FIRST_EVENT = "first_event"
REASON_ARRIVAL_OVERRIDE = "arrival_time_override"
REASON_FIRST_CANCELLED = "first_event_cancelled"
REASON_LOCKED_AFTER_DEPARTURE = "locked_after_departure"

# Attendance values that close the day entirely (spec §5.3, §5.4).
_CLOSING_ATTENDANCE = frozenset(("off", "sick", "remote"))


def _disappeared_uids(
    previous: tuple[ScheduleEvent, ...] | None,
    current: tuple[ScheduleEvent, ...],
) -> frozenset[str]:
    """UIDs present in ``previous`` but absent from ``current``.

    SchoolSoft signals a cancelled lesson by dropping its UID rather than
    emitting ``STATUS:CANCELLED`` (spec §5.2). Mirrors
    :func:`providers.ics.disappeared_uids` but tolerates ``previous=None``.
    """
    if not previous:
        return frozenset()
    previous_uids = {e.uid for e in previous if e.uid}
    current_uids = {e.uid for e in current if e.uid}
    return frozenset(previous_uids - current_uids)


def _sorted_events(events: tuple[ScheduleEvent, ...]) -> list[ScheduleEvent]:
    """Return the day's events sorted by start (providers already sort, but a
    stable order here keeps selection independent of provider behaviour)."""
    return sorted(events, key=lambda e: e.start)


def _is_locked(state: MissionState | None) -> bool:
    """True when the morning mission is locked after a confirmed departure.

    Only an explicit ``reopen_today`` (recorded as ``reopened``) unlocks it;
    ordinary calendar updates do not (spec §5.3).
    """
    return state is not None and state.status == "departed" and not state.reopened


def _select_event(
    events: tuple[ScheduleEvent, ...],
    previous_events: tuple[ScheduleEvent, ...] | None,
    *,
    locked: bool,
) -> tuple[ScheduleEvent | None, bool]:
    """Pick the day's first valid event.

    Returns ``(event, moved_past_cancelled)``. When locked, the first event is
    taken as-is so a schedule change cannot move the start. Before departure, a
    first event whose UID disappeared since ``previous_events`` is treated as a
    possible cancellation and skipped in favour of the next lesson.
    """
    ordered = _sorted_events(events)
    if locked:
        return (ordered[0] if ordered else None), False

    chosen = ordered[0] if ordered else None

    # A possible cancellation (§5.2): the previous day's first lesson vanished
    # from the new fetch. Because the vanished UID is no longer among ``events``
    # it cannot be skipped here; instead flag ``moved`` when a disappeared event
    # started no later than the chosen one, i.e. the start really moved forward.
    gone = _disappeared_uids(previous_events, events)
    moved = False
    if gone and previous_events:
        previous_sorted = _sorted_events(previous_events)
        for prev in previous_sorted:
            if prev.uid not in gone:
                continue
            if chosen is None or prev.start <= chosen.start:
                moved = True
            break
    return chosen, moved


def select_requirement(
    profile: ProfileConfig,
    d: date,
    result: ScheduleResult,
    override: DayOverride | None,
    holiday: bool,
    previous_events: tuple[ScheduleEvent, ...] | None,
    state: MissionState | None,
    now: datetime,
) -> RequirementOutcome:
    """Select the arrival requirement (or day status) for ``profile`` on ``d``.

    See the module docstring for the ordering of rules. ``now`` is accepted for
    purity and future use; selection itself does not branch on the clock
    because the first start is chosen from the whole local day (§5.3).
    """
    locked = _is_locked(state)

    # 1. Closing day override wins over everything, even a locked mission: a
    #    sick day closes the mission and all timers (§5.3).
    if override is not None and override.attendance in _CLOSING_ATTENDANCE:
        return RequirementOutcome(
            day_status="day_off",
            reason_codes=(REASON_OVERRIDE_OFF,),
        )

    # A locked mission keeps its requirement regardless of later schedule
    # changes; rebuild it from the current schedule's first event as-is.
    if not locked:
        # 2. Days outside the weekday mask are silent "no activity" (§5.4).
        if d.weekday() not in profile.weekday_mask:
            return RequirementOutcome(
                day_status="no_activity",
                reason_codes=(REASON_NOT_EXPECTED_DAY,),
            )

        # 3. Explicit holiday ("Ledig") is a distinct day off (§5.4).
        if holiday:
            return RequirementOutcome(
                day_status="day_off",
                reason_codes=(REASON_HOLIDAY,),
            )

    # 4. A fetch error is never a confirmed empty day; surface it so stale
    #    cache can be used and the dashboard shows "could not fetch" (§5.4).
    if result.status == "error":
        error_reasons = [REASON_SOURCE_ERROR]
        if result.stale:
            error_reasons.append(REASON_STALE_CACHE)
        cached_requirement = _build_requirement(
            profile, d, result.events, override, locked
        )
        # Keep a usable requirement from stale cache (if any) while flagging the
        # error so quality/notifications can downgrade appropriately.
        return RequirementOutcome(
            day_status="source_error",
            requirement=cached_requirement,
            reason_codes=tuple(error_reasons),
        )

    # 5. Pick the first valid event of the whole day.
    event, moved = _select_event(result.events, previous_events, locked=locked)
    if event is None:
        # 6. Successful fetch with nothing to attend: "nothing registered" on an
        #    expected day, distinct from a day off (§5.4).
        return RequirementOutcome(
            day_status="no_schedule",
            reason_codes=(REASON_EMPTY_SCHEDULE,),
        )

    requirement = _requirement_from_event(profile, d, event, override)
    reasons: list[str] = [REASON_FIRST_EVENT]
    if override is not None and override.arrival_time is not None:
        reasons.append(REASON_ARRIVAL_OVERRIDE)
    if moved:
        reasons.append(REASON_FIRST_CANCELLED)
    if locked:
        reasons.append(REASON_LOCKED_AFTER_DEPARTURE)
    return RequirementOutcome(
        day_status="has_event",
        requirement=requirement,
        reason_codes=tuple(reasons),
    )


def _build_requirement(
    profile: ProfileConfig,
    d: date,
    events: tuple[ScheduleEvent, ...],
    override: DayOverride | None,
    locked: bool,
) -> ArrivalRequirement | None:
    """Build a requirement from the first event, or ``None`` if there is none.

    Used for the stale-cache path of a source error, where there is no
    ``previous_events`` comparison to make.
    """
    event, _moved = _select_event(events, None, locked=locked)
    if event is None:
        return None
    return _requirement_from_event(profile, d, event, override)


def _requirement_from_event(
    profile: ProfileConfig,
    d: date,
    event: ScheduleEvent,
    override: DayOverride | None,
) -> ArrivalRequirement:
    """Turn the chosen first event into an :class:`ArrivalRequirement`.

    ``override.arrival_time`` replaces the event start (combined in local time).
    ``arrival_deadline = start − margins.arrival`` (spec §4.3, §5.3): for the
    kids the arrival margin buys time to the classroom; for adults it is 0
    because the calendar time is already the arrival requirement.
    """
    if override is not None and override.arrival_time is not None:
        event_start = combine_local(d, override.arrival_time)
    else:
        event_start = event.start

    arrival_deadline = event_start - timedelta(minutes=profile.margins.arrival)
    return ArrivalRequirement(
        mission_id=make_mission_id(profile.id, d),
        person_id=profile.id,
        local_date=d,
        event_id=event.uid,
        event_start=event_start,
        arrival_deadline=arrival_deadline,
        destination_id=profile.destination_id,
        source=event.source_id,
    )
