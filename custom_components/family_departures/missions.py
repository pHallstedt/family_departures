"""Multi-mission selection, resolution and conflict annotation (plan §4.3-§4.7).

A profile has at most one ``main`` calendar link (the school: first-lesson
semantics, slot ``morning``) and any number of ``extra`` links (activities: one
mission per event, slot ``x-<link_id>-<key>``). :func:`select_missions` turns
the day's *already link-filtered* schedule results into a :class:`DayMissions`.

This module is pure (plan §1 rule 3): no ``homeassistant`` import, ``now`` is a
parameter. Event filtering (``calendar_links.filter_for_link``) happens in the
caller, so ``link_days`` holds each link's own view of the day.

Rules:

* The main link delegates to :func:`schedule.select_requirement` unchanged. The
  weekday mask, holiday, ``arrival_time`` override, disappeared-UID detection
  and locking apply to the main link only; an extra event on a holiday or a
  weekend is still a mission.
* Attendance: ``off``/``sick`` close every mission; ``remote`` closes the main
  mission only (remote study does not cancel the evening's football).
* An extra-link error produces no missions for that link and adds
  ``extra_source_error``; it never changes the main outcome and a main error
  never hides extra missions. Extra links have no expectation, so an empty
  extra calendar is silent.
* Locking is per mission. Only the main mission has a lock; an extra mission is
  rebuilt from its current event, and its stable id keeps its ledger.

Profile day status (the scenario table is the spec; it refines plan §4.3):

1. a closing attendance (``off``/``sick``) → ``day_off``;
2. the main link's outcome is ``source_error`` → ``source_error`` (extras are
   still planned, the error is never hidden);
3. any mission exists → ``has_event`` (so a Saturday with only football, or a
   holiday with football, is ``has_event`` although main is silent);
4. otherwise the main link's status, or ``no_activity`` without a main link.

Destination, mode and conflicts are resolved here too (§4.7, §4.6). The main
requirement is left exactly as :func:`select_requirement` builds it unless a
tag, rule or link default actually changes the destination or mode, so a
school-only day is identical to the single-calendar behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from .event_tags import resolve_mode_alias, strip_tags
from .models import (
    ArrivalRequirement,
    CalendarLink,
    DayOverride,
    DayStatus,
    DeparturePlan,
    Location,
    MissionState,
    Mode,
    ProfileConfig,
    RequirementOutcome,
    ScheduleEvent,
    ScheduleResult,
)
from .packing import _rule_matches
from .schedule import REASON_OVERRIDE_OFF, select_requirement
from .timeutil import TZ, make_mission_id

REASON_EXTRA_EVENT = "extra_event"
REASON_EXTRA_SOURCE_ERROR = "extra_source_error"
REASON_UNKNOWN_LOCATION_TAG = "unknown_location_tag"
REASON_UNKNOWN_TRANSPORT_TAG = "unknown_transport_tag"
REASON_UNKNOWN_LINK_LOCATION = "unknown_link_location"
WARNING_HOME_GAP_TOO_SHORT = "home_gap_too_short"
WARNING_OVERLAPS_OTHER = "overlaps_other_commitment"
CONFLICT_WARNINGS = frozenset((WARNING_HOME_GAP_TOO_SHORT, WARNING_OVERLAPS_OTHER))

# Attendance values that close every mission of the day (``remote`` only closes
# the main mission and is handled by ``select_requirement``).
_CLOSES_ALL = frozenset(("off", "sick"))


@dataclass(frozen=True, slots=True)
class MissionOutcome:
    """One mission's outcome. ``outcome.requirement`` is ``None`` for a silent
    or closed main mission, which still carries the main day status."""

    link_id: str
    outcome: RequirementOutcome
    event: ScheduleEvent | None = None


@dataclass(frozen=True, slots=True)
class DayMissions:
    """All missions of one profile day plus the aggregated day status."""

    day_status: DayStatus
    missions: tuple[MissionOutcome, ...] = ()
    reason_codes: tuple[str, ...] = ()
    # Extra links whose fetch failed: their missions are unknown, not removed.
    failed_links: tuple[str, ...] = ()

    @property
    def requirements(self) -> tuple[ArrivalRequirement, ...]:
        """The concrete requirements, main first, then extras in link order."""
        return tuple(
            m.outcome.requirement
            for m in self.missions
            if m.outcome.requirement is not None
        )


# --- Resolution (§4.7) -------------------------------------------------------


def _match_location(value: str, locations: Mapping[str, Location]) -> str | None:
    """Casefolded exact match of ``value`` on a location id, name or alias."""
    folded = value.strip().casefold()
    for location in locations.values():
        names = (location.id, location.name, *location.aliases)
        if any(name.casefold() == folded for name in names):
            return location.id
    return None


def _match_event_location(
    raw: str | None, locations: Mapping[str, Location]
) -> str | None:
    """The location whose longest alias is contained in the event LOCATION."""
    if not raw:
        return None
    folded = raw.casefold()
    best_id: str | None = None
    best_len = 0
    for location in locations.values():
        for alias in location.aliases:
            alias_folded = alias.casefold()
            if alias_folded and alias_folded in folded and len(alias_folded) > best_len:
                best_id, best_len = location.id, len(alias_folded)
    return best_id


def resolve_destination(
    profile: ProfileConfig,
    link: CalendarLink | None,
    event: ScheduleEvent | None,
    locations: Mapping[str, Location],
) -> tuple[str, tuple[str, ...]]:
    """Resolve the destination location id and the reason codes it produced.

    Precedence: event tag ``plats:`` > link rule location > LOCATION alias
    match (only with ``link.match_event_location``) > ``link.location_id`` >
    ``profile.default_location_id``. An unknown tag value or a deleted
    location falls through to the next level with a reason code.
    """
    reasons: list[str] = []

    if event is not None and event.tags.location is not None:
        found = _match_location(event.tags.location, locations)
        if found is not None:
            return found, tuple(reasons)
        reasons.append(REASON_UNKNOWN_LOCATION_TAG)

    if link is not None and event is not None:
        for rule in link.rules:
            if rule.location_id is None or not _rule_matches(event.summary, rule.match):
                continue
            if rule.location_id in locations:
                return rule.location_id, tuple(reasons)
            reasons.append(REASON_UNKNOWN_LINK_LOCATION)
            break

    if link is not None and link.match_event_location and event is not None:
        found = _match_event_location(event.location, locations)
        if found is not None:
            return found, tuple(reasons)

    if link is not None and link.location_id is not None:
        if link.location_id in locations:
            return link.location_id, tuple(reasons)
        reasons.append(REASON_UNKNOWN_LINK_LOCATION)

    return profile.default_location_id, tuple(reasons)


def resolve_mode(
    link: CalendarLink | None,
    event: ScheduleEvent | None,
    override: DayOverride | None,
) -> tuple[Mode | None, tuple[str, ...]]:
    """Resolve an explicit mode and the reason codes it produced.

    Precedence: ``DayOverride.mode`` > event tag ``färdsätt:`` > link rule mode
    > ``link.mode``. ``None`` means "nothing overrides", so the caller uses
    ``profile.default_mode``. An unknown tag value falls through with
    ``unknown_transport_tag``.
    """
    reasons: list[str] = []

    if override is not None and override.mode is not None:
        return override.mode, ()

    if event is not None and event.tags.transport is not None:
        mode = resolve_mode_alias(event.tags.transport)
        if mode is not None:
            return mode, ()
        reasons.append(REASON_UNKNOWN_TRANSPORT_TAG)

    if link is not None and event is not None:
        for rule in link.rules:
            if rule.mode is not None and _rule_matches(event.summary, rule.match):
                return rule.mode, tuple(reasons)

    if link is not None and link.mode is not None:
        return link.mode, tuple(reasons)

    return None, tuple(reasons)


# --- Selection (§4.3) --------------------------------------------------------


def _error_result(now: datetime) -> ScheduleResult:
    """Stand-in for a link whose fetch result is missing from ``link_days``."""
    return ScheduleResult(
        status="error", events=(), fetched_at=now, error_code="no_result"
    )


def _unique(codes: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(codes))


def _key_for(event: ScheduleEvent) -> str:
    """Occurrence key, falling back to the local start ``HHMM`` (§4.4)."""
    if event.occurrence_key:
        return event.occurrence_key
    return event.start.astimezone(TZ).strftime("%H%M")


def _enrich_main(
    profile: ProfileConfig,
    link: CalendarLink,
    result: ScheduleResult,
    outcome: RequirementOutcome,
    override: DayOverride | None,
    locations: Mapping[str, Location],
) -> RequirementOutcome:
    """Apply destination/mode resolution to the main requirement."""
    requirement = outcome.requirement
    if requirement is None:
        return outcome
    event = _main_event(result, outcome)
    destination_id, dest_reasons = resolve_destination(profile, link, event, locations)
    mode, mode_reasons = resolve_mode(link, event, override)
    return replace(
        outcome,
        requirement=replace(requirement, destination_id=destination_id, mode=mode),
        reason_codes=_unique((*outcome.reason_codes, *dest_reasons, *mode_reasons)),
    )


def _main_event(
    result: ScheduleResult, outcome: RequirementOutcome
) -> ScheduleEvent | None:
    requirement = outcome.requirement
    if requirement is None:
        return None
    # uid is unique per event except for same-summary HA events; those start at
    # the same time, so the first match is the right one for first-event selection.
    return next((e for e in result.events if e.uid == requirement.event_id), None)


def _extra_requirements(
    profile: ProfileConfig,
    link: CalendarLink,
    d: date,
    result: ScheduleResult,
    override: DayOverride | None,
    locations: Mapping[str, Location],
) -> tuple[MissionOutcome, ...]:
    """One mission per event of an extra link, in start order."""
    margin = (
        link.arrival_margin
        if link.arrival_margin is not None
        else profile.margins.arrival
    )
    seen: set[str] = set()
    outcomes: list[MissionOutcome] = []
    for event in sorted(result.events, key=lambda e: e.start):
        slot = link.slot_for(_key_for(event))
        suffix = 2
        unique_slot = slot
        while unique_slot in seen:
            unique_slot = f"{slot}-{suffix}"
            suffix += 1
        seen.add(unique_slot)

        destination_id, dest_reasons = resolve_destination(
            profile, link, event, locations
        )
        mode, mode_reasons = resolve_mode(link, event, override)
        label = strip_tags(event.summary) if link.show_summary else link.label
        requirement = ArrivalRequirement(
            mission_id=make_mission_id(profile.id, d, unique_slot),
            person_id=profile.id,
            local_date=d,
            event_id=event.uid,
            event_start=event.start,
            arrival_deadline=event.start - timedelta(minutes=margin),
            destination_id=destination_id,
            source=event.source_id,
            slot=unique_slot,
            link_id=link.link_id,
            mode=mode,
            label=label or None,
            destination_name=(
                locations[destination_id].name if destination_id in locations else None
            ),
        )
        outcomes.append(
            MissionOutcome(
                link_id=link.link_id,
                event=event,
                outcome=RequirementOutcome(
                    day_status="has_event",
                    requirement=requirement,
                    reason_codes=_unique(
                        (REASON_EXTRA_EVENT, *dest_reasons, *mode_reasons)
                    ),
                ),
            )
        )
    return tuple(outcomes)


def select_missions(
    profile: ProfileConfig,
    d: date,
    link_days: Mapping[str, ScheduleResult],
    override: DayOverride | None,
    holiday: bool,
    previous_main_events: tuple[ScheduleEvent, ...] | None,
    states: Mapping[str, MissionState],
    locations: Mapping[str, Location],
    now: datetime,
) -> DayMissions:
    """Select every mission of ``profile`` on ``d``.

    ``link_days`` maps ``link_id`` to that link's already filtered
    :class:`ScheduleResult`. A link missing from it counts as a fetch error.
    ``states`` maps mission ids to their persisted state (only the main
    mission's lock is consulted).
    """
    main = profile.main_link
    closes_all = override is not None and override.attendance in _CLOSES_ALL
    missions: list[MissionOutcome] = []
    reasons: list[str] = []
    failed_links: list[str] = []
    main_status: DayStatus | None = None

    if main is not None:
        main_result = link_days.get(main.link_id) or _error_result(now)
        main_outcome = select_requirement(
            profile,
            d,
            main_result,
            override,
            holiday,
            previous_main_events,
            states.get(make_mission_id(profile.id, d)),
            now,
            main,
        )
        main_outcome = _enrich_main(
            profile, main, main_result, main_outcome, override, locations
        )
        main_status = main_outcome.day_status
        missions.append(
            MissionOutcome(
                main.link_id, main_outcome, _main_event(main_result, main_outcome)
            )
        )
        reasons.extend(main_outcome.reason_codes)

    if closes_all:
        if main is None:
            reasons.append(REASON_OVERRIDE_OFF)
        return DayMissions("day_off", tuple(missions), _unique(reasons))

    for link in profile.calendar_links:
        if link.role != "extra":
            continue
        result = link_days.get(link.link_id) or _error_result(now)
        if result.status == "error":
            reasons.append(REASON_EXTRA_SOURCE_ERROR)
            failed_links.append(link.link_id)
            continue
        missions.extend(
            _extra_requirements(profile, link, d, result, override, locations)
        )

    has_requirement = any(m.outcome.requirement is not None for m in missions)
    if main_status == "source_error":
        day_status: DayStatus = "source_error"
    elif has_requirement:
        day_status = "has_event"
    elif main_status is not None:
        day_status = main_status
    else:
        day_status = "no_activity"
    return DayMissions(
        day_status, tuple(missions), _unique(reasons), tuple(failed_links)
    )


# --- Conflicts (§4.6) --------------------------------------------------------


def _event_key(
    event: ScheduleEvent,
) -> tuple[str, str, str | None, str, datetime, datetime]:
    """Identity of an event. HA calendar uids derive from summary and start, so
    two same-summary simultaneous events share a uid; ``occurrence_key`` (which
    carries the ``-2`` suffix) keeps them apart."""
    return (
        event.source_id,
        event.uid,
        event.occurrence_key,
        event.summary,
        event.start,
        event.end,
    )


def _overlaps(a: ScheduleEvent, b: ScheduleEvent) -> bool:
    if a.start == b.start:
        return True
    return a.start < b.end and b.start < a.end


def annotate_conflicts(
    plans: Sequence[DeparturePlan],
    mission_events: Mapping[str, ScheduleEvent],
    day_events: Sequence[ScheduleEvent],
) -> tuple[DeparturePlan, ...]:
    """Flag back-to-back and overlapping extra missions (plan §4.6).

    ``mission_events`` maps a mission id to its event; ``day_events`` is every
    linked event of the profile that day (main lessons included). Only extra
    missions are annotated, so school lessons never flag each other. A flag is
    added to ``requirement.warnings`` and the plan's ``reason_codes`` and never
    changes the plan itself (O2):

    * ``overlaps_other_commitment``: the event overlaps any other linked event;
    * ``home_gap_too_short``: the recommended leave is before the end of the
      latest other event that ends at or before this event's start, so going
      home in between is not realistic.
    """
    annotated: list[DeparturePlan] = []
    for plan in plans:
        requirement = plan.requirement
        event = mission_events.get(plan.mission_id)
        if not requirement.slot.startswith("x-") or event is None:
            annotated.append(plan)
            continue

        own = _event_key(event)
        others = [e for e in day_events if _event_key(e) != own]
        flags: list[str] = []
        if any(_overlaps(event, other) for other in others):
            flags.append(WARNING_OVERLAPS_OTHER)
        earlier_ends = [
            other.end
            for other in others
            if other.end <= event.start and not _overlaps(event, other)
        ]
        if (
            plan.recommended_leave is not None
            and earlier_ends
            and plan.recommended_leave < max(earlier_ends)
        ):
            flags.append(WARNING_HOME_GAP_TOO_SHORT)

        if not flags:
            annotated.append(plan)
            continue
        annotated.append(
            replace(
                plan,
                requirement=replace(
                    requirement, warnings=_unique((*requirement.warnings, *flags))
                ),
                reason_codes=_unique((*plan.reason_codes, *flags)),
            )
        )
    return tuple(annotated)
