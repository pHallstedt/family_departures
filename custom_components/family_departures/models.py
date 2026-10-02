"""Domain models for the Family Departures integration.

These are the frozen contracts the pure planner, schedule, packing and
notification modules code against (plan §3). They must not import
``homeassistant`` so the planning logic stays testable without a running
Home Assistant instance.

Rules enforced here:

* Datetimes are timezone-aware; naive datetimes are rejected. Internally they
  are UTC (see :mod:`.timeutil`), but any aware datetime is accepted so callers
  may pass values in other zones.
* Minute-valued margins are non-negative integers.
* ``mode``/``attendance`` and the various status literals are validated against
  their allowed values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Literal, get_args

Mode = Literal["public_transport", "car", "static"]
Attendance = Literal["normal", "off", "remote", "sick"]
Quality = Literal["realtime", "scheduled", "estimated", "stale", "unavailable"]
MissionStatus = Literal[
    "no_event",
    "scheduled",
    "preparing",
    "leave_now",
    "late",
    "departed",
    "skipped",
    "cannot_arrive_on_time",
    "needs_configuration",
]
DayStatus = Literal[
    "has_event",
    "no_activity",
    "day_off",
    "no_schedule",
    "source_error",
]

_MODES: frozenset[str] = frozenset(get_args(Mode))
_ATTENDANCES: frozenset[str] = frozenset(get_args(Attendance))
_QUALITIES: frozenset[str] = frozenset(get_args(Quality))
_MISSION_STATUSES: frozenset[str] = frozenset(get_args(MissionStatus))
_DAY_STATUSES: frozenset[str] = frozenset(get_args(DayStatus))
_SCHEDULE_STATUSES: frozenset[str] = frozenset(("ok", "empty", "error"))
_JOURNEY_STATUSES: frozenset[str] = frozenset(("ok", "empty", "error"))
_LEG_KINDS: frozenset[str] = frozenset(("walk", "transit"))
_DURATION_SOURCES: frozenset[str] = frozenset(("waze", "fallback", "static"))


def _require_aware(value: datetime, field_name: str) -> None:
    """Reject naive datetimes."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware, got naive {value!r}")


def _require_aware_optional(value: datetime | None, field_name: str) -> None:
    if value is not None:
        _require_aware(value, field_name)


def _require_in(value: str, allowed: frozenset[str], field_name: str) -> None:
    if value not in allowed:
        raise ValueError(
            f"{field_name} must be one of {sorted(allowed)}, got {value!r}"
        )


def _require_non_negative(value: int, field_name: str) -> None:
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative, got {value}")


@dataclass(frozen=True, slots=True)
class ScheduleEvent:
    """A single calendar/schedule event, with aware UTC start and end."""

    uid: str
    summary: str
    start: datetime
    end: datetime
    source_id: str

    def __post_init__(self) -> None:
        _require_aware(self.start, "ScheduleEvent.start")
        _require_aware(self.end, "ScheduleEvent.end")
        if self.end < self.start:
            raise ValueError(
                "ScheduleEvent.end must not be before start "
                f"({self.end!r} < {self.start!r})"
            )


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    """The outcome of fetching one day's schedule from a source."""

    status: Literal["ok", "empty", "error"]
    events: tuple[ScheduleEvent, ...]
    fetched_at: datetime
    source_modified_at: datetime | None = None
    content_hash: str | None = None
    error_code: str | None = None
    stale: bool = False

    def __post_init__(self) -> None:
        _require_in(self.status, _SCHEDULE_STATUSES, "ScheduleResult.status")
        _require_aware(self.fetched_at, "ScheduleResult.fetched_at")
        _require_aware_optional(
            self.source_modified_at, "ScheduleResult.source_modified_at"
        )


@dataclass(frozen=True, slots=True)
class SourceFilter:
    """Case-insensitive include/exclude summary patterns for a schedule source."""

    exclude_patterns: tuple[str, ...] = ()
    include_patterns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PackingRule:
    """Rule mapping a matched event summary to a packing-list item."""

    id: str
    match: str
    item: str


@dataclass(frozen=True, slots=True)
class PackingList:
    """A person's packing list for one local date."""

    person_id: str
    local_date: date
    items: tuple[str, ...] = ()
    acknowledged: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Margins:
    """Per-profile margins in whole minutes (plan §3, spec §7)."""

    arrival: int
    departure: int
    boarding: int
    parking_and_walk: int
    min_transfer: int

    def __post_init__(self) -> None:
        _require_non_negative(self.arrival, "Margins.arrival")
        _require_non_negative(self.departure, "Margins.departure")
        _require_non_negative(self.boarding, "Margins.boarding")
        _require_non_negative(self.parking_and_walk, "Margins.parking_and_walk")
        _require_non_negative(self.min_transfer, "Margins.min_transfer")


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    """Persistent configuration for one profile.

    The ICS URL is intentionally absent: it lives in ``entry.data`` and is read
    only by the coordinator (plan §3, review-privacy-security).
    """

    id: str
    name: str
    source_type: Literal["ics", "ha_calendar"]
    calendar_entity_id: str | None
    source_filter: SourceFilter
    destination_id: str
    dest_lat: float
    dest_lon: float
    default_mode: Mode
    static_minutes: int | None
    static_label: str | None
    weather_adjust: bool
    car_fallback_minutes: int | None
    margins: Margins
    weekday_mask: frozenset[int]
    packing_rules: tuple[PackingRule, ...]
    person_entity_id: str | None
    notifications_enabled: bool
    change_threshold_minutes: int
    quiet_start: time
    quiet_end: time
    scripts: Mapping[str, str] = field(default_factory=dict)
    # When False, the 20:00 evening summary is suppressed and only a
    # packing-only notice is sent if a packing list exists (spec §5.5, §12.1).
    # Defaults to True so existing/synthetic ProfileConfig construction stays
    # valid; the options flow (T12) exposes the toggle. Added to the T02
    # contract by approval during T15.
    evening_notice_enabled: bool = True

    def __post_init__(self) -> None:
        _require_in(self.source_type, frozenset(("ics", "ha_calendar")), "source_type")
        _require_in(self.default_mode, _MODES, "ProfileConfig.default_mode")
        if self.static_minutes is not None:
            _require_non_negative(self.static_minutes, "ProfileConfig.static_minutes")
        if self.car_fallback_minutes is not None:
            _require_non_negative(
                self.car_fallback_minutes, "ProfileConfig.car_fallback_minutes"
            )
        _require_non_negative(
            self.change_threshold_minutes, "ProfileConfig.change_threshold_minutes"
        )
        for day in self.weekday_mask:
            if not 0 <= day <= 6:
                raise ValueError(
                    f"ProfileConfig.weekday_mask values must be 0-6, got {day}"
                )


@dataclass(frozen=True, slots=True)
class DayOverride:
    """A per-day override for a person's mode, attendance or arrival time."""

    person_id: str
    local_date: date
    mode: Mode | None = None
    attendance: Attendance | None = None
    arrival_time: time | None = None

    def __post_init__(self) -> None:
        if self.mode is not None:
            _require_in(self.mode, _MODES, "DayOverride.mode")
        if self.attendance is not None:
            _require_in(self.attendance, _ATTENDANCES, "DayOverride.attendance")


@dataclass(frozen=True, slots=True)
class ArrivalRequirement:
    """What arrival the plan must satisfy for a mission."""

    mission_id: str
    person_id: str
    local_date: date
    event_id: str
    event_start: datetime
    arrival_deadline: datetime
    destination_id: str
    source: str

    def __post_init__(self) -> None:
        _require_aware(self.event_start, "ArrivalRequirement.event_start")
        _require_aware(self.arrival_deadline, "ArrivalRequirement.arrival_deadline")


@dataclass(frozen=True, slots=True)
class RequirementOutcome:
    """Result of selecting the day's requirement (plan §3)."""

    day_status: DayStatus
    requirement: ArrivalRequirement | None = None
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_in(self.day_status, _DAY_STATUSES, "RequirementOutcome.day_status")


@dataclass(frozen=True, slots=True)
class Leg:
    """One leg of a journey (walk or transit)."""

    kind: Literal["walk", "transit"]
    line: str | None = None
    direction: str | None = None
    from_stop: str | None = None
    to_stop: str | None = None
    platform: str | None = None
    planned_departure: datetime | None = None
    estimated_departure: datetime | None = None
    planned_arrival: datetime | None = None
    estimated_arrival: datetime | None = None
    cancelled: bool = False
    trip_ref: str | None = None

    def __post_init__(self) -> None:
        _require_in(self.kind, _LEG_KINDS, "Leg.kind")
        _require_aware_optional(self.planned_departure, "Leg.planned_departure")
        _require_aware_optional(self.estimated_departure, "Leg.estimated_departure")
        _require_aware_optional(self.planned_arrival, "Leg.planned_arrival")
        _require_aware_optional(self.estimated_arrival, "Leg.estimated_arrival")


@dataclass(frozen=True, slots=True)
class Journey:
    """A full journey made of ordered legs."""

    journey_id: str
    legs: tuple[Leg, ...]


@dataclass(frozen=True, slots=True)
class JourneyResult:
    """The outcome of a journey-planner query."""

    status: Literal["ok", "empty", "error"]
    journeys: tuple[Journey, ...]
    fetched_at: datetime
    has_realtime: bool = False
    error_code: str | None = None

    def __post_init__(self) -> None:
        _require_in(self.status, _JOURNEY_STATUSES, "JourneyResult.status")
        _require_aware(self.fetched_at, "JourneyResult.fetched_at")


@dataclass(frozen=True, slots=True)
class DurationResult:
    """A door-to-door travel duration from a car/static source."""

    minutes: float | None
    fetched_at: datetime
    source: Literal["waze", "fallback", "static"]
    quality: Quality
    route_name: str | None = None

    def __post_init__(self) -> None:
        _require_in(self.source, _DURATION_SOURCES, "DurationResult.source")
        _require_in(self.quality, _QUALITIES, "DurationResult.quality")
        _require_aware(self.fetched_at, "DurationResult.fetched_at")
        if self.minutes is not None and self.minutes < 0:
            raise ValueError(f"DurationResult.minutes must be >= 0, got {self.minutes}")


@dataclass(frozen=True, slots=True)
class MarginBreakdown:
    """The stacked margins behind a plan, for dashboard/shadow-mode (spec §7)."""

    travel: int
    access_walk: int
    boarding: int
    departure: int
    arrival: int
    extra_after: int

    def __post_init__(self) -> None:
        _require_non_negative(self.travel, "MarginBreakdown.travel")
        _require_non_negative(self.access_walk, "MarginBreakdown.access_walk")
        _require_non_negative(self.boarding, "MarginBreakdown.boarding")
        _require_non_negative(self.departure, "MarginBreakdown.departure")
        _require_non_negative(self.arrival, "MarginBreakdown.arrival")
        _require_non_negative(self.extra_after, "MarginBreakdown.extra_after")


@dataclass(frozen=True, slots=True)
class DeparturePlan:
    """A computed departure plan for one mission."""

    mission_id: str
    plan_id: str
    revision: int
    requirement: ArrivalRequirement
    mode: Mode
    recommended_leave: datetime | None
    latest_leave: datetime | None
    last_on_time_alternative_leave: datetime | None
    predicted_arrival: datetime | None
    journey_id: str | None
    route_summary: str | None
    quality: Quality
    feasible: bool
    status: MissionStatus
    breakdown: MarginBreakdown | None
    reason_codes: tuple[str, ...]
    config_revision: int

    def __post_init__(self) -> None:
        _require_in(self.mode, _MODES, "DeparturePlan.mode")
        _require_in(self.quality, _QUALITIES, "DeparturePlan.quality")
        _require_in(self.status, _MISSION_STATUSES, "DeparturePlan.status")
        _require_aware_optional(
            self.recommended_leave, "DeparturePlan.recommended_leave"
        )
        _require_aware_optional(self.latest_leave, "DeparturePlan.latest_leave")
        _require_aware_optional(
            self.last_on_time_alternative_leave,
            "DeparturePlan.last_on_time_alternative_leave",
        )
        _require_aware_optional(
            self.predicted_arrival, "DeparturePlan.predicted_arrival"
        )


@dataclass(frozen=True, slots=True)
class NotifiedRecord:
    """Ledger entry for a notification already sent for a mission."""

    kind: str
    sent_at: datetime
    leave_time: datetime | None
    revision: int

    def __post_init__(self) -> None:
        _require_aware(self.sent_at, "NotifiedRecord.sent_at")
        _require_aware_optional(self.leave_time, "NotifiedRecord.leave_time")


@dataclass(frozen=True, slots=True)
class MissionState:
    """Per-mission runtime state, keyed on ``mission_id`` (spec §8)."""

    mission_id: str
    status: MissionStatus
    departed_at: datetime | None
    reopened: bool
    notified: Mapping[str, NotifiedRecord]
    first_published_leave: datetime | None
    action_nonce: str

    def __post_init__(self) -> None:
        _require_in(self.status, _MISSION_STATUSES, "MissionState.status")
        _require_aware_optional(self.departed_at, "MissionState.departed_at")
        _require_aware_optional(
            self.first_published_leave, "MissionState.first_published_leave"
        )


@dataclass(frozen=True, slots=True)
class NotificationIntent:
    """A notification the policy decided to send (plan §3, spec §12)."""

    person_id: str
    mission_id: str
    plan_id: str
    revision: int
    notification_id: str
    kind: str
    severity: str
    title: str
    message: str
    recommended_leave_time: datetime | None
    latest_leave_time: datetime | None
    quality: Quality
    reason_codes: tuple[str, ...]
    packing_items: tuple[str, ...]
    channels: tuple[str, ...]
    tag: str
    action_nonce: str

    def __post_init__(self) -> None:
        _require_in(self.quality, _QUALITIES, "NotificationIntent.quality")
        _require_aware_optional(
            self.recommended_leave_time, "NotificationIntent.recommended_leave_time"
        )
        _require_aware_optional(
            self.latest_leave_time, "NotificationIntent.latest_leave_time"
        )
