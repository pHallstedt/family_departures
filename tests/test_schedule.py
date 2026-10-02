"""Tests for first-event selection and day locking (spec §5.3, §5.4, §16).

These exercise product behaviour from the §5.4 "Situation/Beteende" table and
the §16 scenario rows that concern schedule selection, with controlled time and
synthetic events (no real schedule content).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from custom_components.family_departures.models import (
    DayOverride,
    Margins,
    MissionState,
    ProfileConfig,
    ScheduleEvent,
    ScheduleResult,
    SourceFilter,
)
from custom_components.family_departures.schedule import (
    REASON_ARRIVAL_OVERRIDE,
    REASON_EMPTY_SCHEDULE,
    REASON_FIRST_CANCELLED,
    REASON_HOLIDAY,
    REASON_LOCKED_AFTER_DEPARTURE,
    REASON_NOT_EXPECTED_DAY,
    REASON_OVERRIDE_OFF,
    REASON_SOURCE_ERROR,
    REASON_STALE_CACHE,
    select_requirement,
)

TZ = ZoneInfo("Europe/Stockholm")
NOW = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)  # a Thursday, before departure


def _local(d: date, hh: int, mm: int) -> datetime:
    """Local wall-clock on date ``d`` as aware UTC."""
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ).astimezone(UTC)


def _event(d: date, hh: int, mm: int, *, summary: str, uid: str) -> ScheduleEvent:
    start = _local(d, hh, mm)
    return ScheduleEvent(
        uid=uid,
        summary=summary,
        start=start,
        end=_local(d, hh + 1, mm),
        source_id="kid_b_ics",
    )


def _result(
    *events: ScheduleEvent, status: str = "ok", stale: bool = False
) -> ScheduleResult:
    resolved = status if status != "ok" or events else "empty"
    return ScheduleResult(
        status=resolved,  # type: ignore[arg-type]
        events=tuple(events),
        fetched_at=NOW,
        stale=stale,
    )


def _profile(
    *,
    arrival: int = 5,
    weekday_mask: frozenset[int] = frozenset({0, 1, 2, 3, 4}),
) -> ProfileConfig:
    return ProfileConfig(
        id="kid_b",
        name="Kid B",
        source_type="ics",
        calendar_entity_id=None,
        source_filter=SourceFilter(),
        destination_id="school",
        dest_lat=59.3,
        dest_lon=18.0,
        default_mode="public_transport",
        static_minutes=None,
        static_label=None,
        weather_adjust=False,
        car_fallback_minutes=None,
        margins=Margins(
            arrival=arrival,
            departure=0,
            boarding=0,
            parking_and_walk=0,
            min_transfer=5,
        ),
        weekday_mask=weekday_mask,
        packing_rules=(),
        person_entity_id=None,
        notifications_enabled=True,
        change_threshold_minutes=5,
        quiet_start=time(21, 0),
        quiet_end=time(6, 0),
        scripts={},
    )


def _departed_state(mission_id: str = "kid_b:2026-10-01:morning") -> MissionState:
    return MissionState(
        mission_id=mission_id,
        status="departed",
        departed_at=NOW,
        reopened=False,
        notified={},
        first_published_leave=None,
        action_nonce="n",
    )


# --- §16: two lessons and an early task -> first real lesson chosen ----------


def test_first_real_lesson_chosen_from_whole_day() -> None:
    d = date(2026, 10, 1)
    first = _event(d, 8, 20, summary="Lektion MATE", uid="a")
    second = _event(d, 9, 15, summary="Lektion SVEN", uid="b")
    outcome = select_requirement(
        _profile(), d, _result(second, first), None, False, None, None, NOW
    )
    assert outcome.day_status == "has_event"
    assert outcome.requirement is not None
    assert outcome.requirement.event_id == "a"
    assert outcome.requirement.event_start == _local(d, 8, 20)


# --- §5.3: first start chosen from whole day even when 08:20 is passed -------


def test_passed_first_lesson_does_not_promote_later_lesson() -> None:
    d = date(2026, 10, 1)
    first = _event(d, 8, 20, summary="Lektion MATE", uid="a")
    later = _event(d, 9, 15, summary="Lektion SVEN", uid="b")
    # Even if "now" is after 08:20 local, the day's first start stays 08:20.
    late_now = _local(d, 8, 40)
    outcome = select_requirement(
        _profile(), d, _result(first, later), None, False, None, None, late_now
    )
    assert outcome.requirement is not None
    assert outcome.requirement.event_start == _local(d, 8, 20)


# --- arrival deadline = start - arrival margin ------------------------------


def test_arrival_deadline_subtracts_kid_margin() -> None:
    d = date(2026, 10, 1)
    first = _event(d, 8, 20, summary="Lektion MATE", uid="a")
    outcome = select_requirement(
        _profile(arrival=5), d, _result(first), None, False, None, None, NOW
    )
    assert outcome.requirement is not None
    assert outcome.requirement.arrival_deadline == _local(d, 8, 15)


def test_adult_arrival_margin_zero_keeps_calendar_time() -> None:
    # §16: Parent A flex calendar 09:00 -> arrival_minutes 0, deadline == start.
    d = date(2026, 10, 1)
    work = _event(d, 9, 0, summary="Jobb", uid="w")
    outcome = select_requirement(
        _profile(arrival=0), d, _result(work), None, False, None, None, NOW
    )
    assert outcome.requirement is not None
    assert outcome.requirement.arrival_deadline == outcome.requirement.event_start
    assert outcome.requirement.event_start == _local(d, 9, 0)


# --- §16: first lesson cancelled before departure (UID disappears) ----------


def test_disappeared_first_lesson_moves_to_next_before_departure() -> None:
    d = date(2026, 10, 1)
    old_first = _event(d, 8, 20, summary="Lektion MATE", uid="a")
    second = _event(d, 9, 15, summary="Lektion SVEN", uid="b")
    previous = (old_first, second)
    # New fetch: UID "a" vanished -> treated as cancellation, move to "b".
    current = _result(second)
    outcome = select_requirement(
        _profile(), d, current, None, False, previous, None, NOW
    )
    assert outcome.requirement is not None
    assert outcome.requirement.event_id == "b"
    assert outcome.requirement.event_start == _local(d, 9, 15)
    assert REASON_FIRST_CANCELLED in outcome.reason_codes


def test_all_candidates_disappeared_is_no_schedule() -> None:
    d = date(2026, 10, 1)
    previous = (_event(d, 8, 20, summary="Lektion MATE", uid="a"),)
    outcome = select_requirement(
        _profile(), d, _result(status="empty"), None, False, previous, None, NOW
    )
    assert outcome.day_status == "no_schedule"


# --- §16: same change after departure -> keep locked requirement ------------


def test_locked_after_departure_keeps_first_event_despite_change() -> None:
    d = date(2026, 10, 1)
    old_first = _event(d, 8, 20, summary="Lektion MATE", uid="a")
    second = _event(d, 9, 15, summary="Lektion SVEN", uid="b")
    previous = (old_first, second)
    # UID "a" vanished, but the mission already departed and is not reopened:
    # the start must not move to "b".
    current = _result(second)
    outcome = select_requirement(
        _profile(),
        d,
        current,
        None,
        False,
        previous,
        _departed_state(),
        NOW,
    )
    assert outcome.day_status == "has_event"
    assert outcome.requirement is not None
    # Locked: take the current first event as-is, no cancellation move.
    assert outcome.requirement.event_id == "b"
    assert REASON_FIRST_CANCELLED not in outcome.reason_codes
    assert REASON_LOCKED_AFTER_DEPARTURE in outcome.reason_codes


def test_reopened_mission_is_not_locked() -> None:
    d = date(2026, 10, 1)
    old_first = _event(d, 8, 20, summary="Lektion MATE", uid="a")
    second = _event(d, 9, 15, summary="Lektion SVEN", uid="b")
    state = MissionState(
        mission_id="kid_b:2026-10-01:morning",
        status="departed",
        departed_at=NOW,
        reopened=True,
        notified={},
        first_published_leave=None,
        action_nonce="n",
    )
    outcome = select_requirement(
        _profile(), d, _result(second), None, False, (old_first, second), state, NOW
    )
    assert outcome.requirement is not None
    assert outcome.requirement.event_id == "b"
    assert REASON_FIRST_CANCELLED in outcome.reason_codes


# --- §5.4 table: empty Saturday vs expected Tuesday -------------------------


def test_empty_day_outside_weekday_mask_is_no_activity() -> None:
    saturday = date(2026, 10, 3)
    outcome = select_requirement(
        _profile(), saturday, _result(status="empty"), None, False, None, None, NOW
    )
    assert outcome.day_status == "no_activity"
    assert REASON_NOT_EXPECTED_DAY in outcome.reason_codes


def test_empty_expected_day_is_no_schedule() -> None:
    tuesday = date(2026, 9, 29)
    outcome = select_requirement(
        _profile(), tuesday, _result(status="empty"), None, False, None, None, NOW
    )
    assert outcome.day_status == "no_schedule"
    assert REASON_EMPTY_SCHEDULE in outcome.reason_codes


# --- §5.4 table: holiday "Ledig" -> day off ---------------------------------


def test_holiday_is_day_off() -> None:
    d = date(2026, 10, 1)
    first = _event(d, 8, 20, summary="Lektion MATE", uid="a")
    outcome = select_requirement(
        _profile(), d, _result(first), None, True, None, None, NOW
    )
    assert outcome.day_status == "day_off"
    assert REASON_HOLIDAY in outcome.reason_codes
    assert outcome.requirement is None


# --- §5.3: off/sick/remote override closes the mission ----------------------


def test_sick_override_closes_day() -> None:
    d = date(2026, 10, 1)
    first = _event(d, 8, 20, summary="Lektion MATE", uid="a")
    override = DayOverride(person_id="kid_b", local_date=d, attendance="sick")
    outcome = select_requirement(
        _profile(), d, _result(first), override, False, None, None, NOW
    )
    assert outcome.day_status == "day_off"
    assert REASON_OVERRIDE_OFF in outcome.reason_codes


def test_sick_override_overrides_lock() -> None:
    # Even a locked (departed) mission is closed by a sick override.
    d = date(2026, 10, 1)
    first = _event(d, 8, 20, summary="Lektion MATE", uid="a")
    override = DayOverride(person_id="kid_b", local_date=d, attendance="remote")
    outcome = select_requirement(
        _profile(),
        d,
        _result(first),
        override,
        False,
        None,
        _departed_state(),
        NOW,
    )
    assert outcome.day_status == "day_off"


def test_normal_attendance_override_does_not_close_day() -> None:
    d = date(2026, 10, 1)
    first = _event(d, 8, 20, summary="Lektion MATE", uid="a")
    override = DayOverride(person_id="kid_b", local_date=d, attendance="normal")
    outcome = select_requirement(
        _profile(), d, _result(first), override, False, None, None, NOW
    )
    assert outcome.day_status == "has_event"


# --- override arrival_time replaces the start -------------------------------


def test_arrival_time_override_replaces_start() -> None:
    d = date(2026, 10, 1)
    first = _event(d, 8, 20, summary="Lektion MATE", uid="a")
    override = DayOverride(person_id="kid_b", local_date=d, arrival_time=time(9, 30))
    outcome = select_requirement(
        _profile(arrival=0), d, _result(first), override, False, None, None, NOW
    )
    assert outcome.requirement is not None
    assert outcome.requirement.event_start == _local(d, 9, 30)
    assert REASON_ARRIVAL_OVERRIDE in outcome.reason_codes


# --- §5.4 table: fetch error vs empty are distinct, never auto day off ------


def test_http_error_on_expected_day_is_source_error() -> None:
    d = date(2026, 10, 1)
    outcome = select_requirement(
        _profile(),
        d,
        _result(status="error", stale=False),
        None,
        False,
        None,
        None,
        NOW,
    )
    assert outcome.day_status == "source_error"
    assert outcome.requirement is None
    assert REASON_SOURCE_ERROR in outcome.reason_codes


def test_source_error_with_stale_cache_keeps_requirement() -> None:
    d = date(2026, 10, 1)
    cached = _event(d, 8, 20, summary="Lektion MATE", uid="a")
    result = ScheduleResult(
        status="error",
        events=(cached,),
        fetched_at=NOW,
        error_code="http_error",
        stale=True,
    )
    outcome = select_requirement(_profile(), d, result, None, False, None, None, NOW)
    assert outcome.day_status == "source_error"
    assert outcome.requirement is not None
    assert outcome.requirement.event_id == "a"
    assert REASON_STALE_CACHE in outcome.reason_codes


def test_error_and_empty_resolve_differently() -> None:
    d = date(2026, 10, 1)
    error = select_requirement(
        _profile(), d, _result(status="error"), None, False, None, None, NOW
    )
    empty = select_requirement(
        _profile(), d, _result(status="empty"), None, False, None, None, NOW
    )
    assert error.day_status == "source_error"
    assert empty.day_status == "no_schedule"
    assert error.day_status != empty.day_status
