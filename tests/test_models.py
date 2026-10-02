"""Tests for the frozen domain models and their validation."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, time

import pytest
from custom_components.family_departures.models import (
    ArrivalRequirement,
    DayOverride,
    DeparturePlan,
    DurationResult,
    JourneyResult,
    Leg,
    MarginBreakdown,
    Margins,
    MissionState,
    ProfileConfig,
    ScheduleEvent,
    ScheduleResult,
    SourceFilter,
)

UTC = UTC
T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 1, 6, 20, tzinfo=UTC)


def _margins() -> Margins:
    return Margins(
        arrival=5, departure=5, boarding=2, parking_and_walk=5, min_transfer=5
    )


def _requirement() -> ArrivalRequirement:
    return ArrivalRequirement(
        mission_id="kid_a:2026-10-01:morning",
        person_id="kid_a",
        local_date=date(2026, 10, 1),
        event_id="e1",
        event_start=datetime(2026, 10, 1, 6, 20, tzinfo=UTC),
        arrival_deadline=datetime(2026, 10, 1, 6, 15, tzinfo=UTC),
        destination_id="school",
        source="ics",
    )


def test_schedule_event_rejects_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ScheduleEvent(
            uid="u",
            summary="s",
            start=datetime(2026, 10, 1, 8, 0),
            end=T1,
            source_id="src",
        )


def test_schedule_event_rejects_end_before_start() -> None:
    with pytest.raises(ValueError, match="before start"):
        ScheduleEvent(uid="u", summary="s", start=T1, end=T0, source_id="src")


def test_schedule_event_is_frozen() -> None:
    event = ScheduleEvent(uid="u", summary="s", start=T0, end=T1, source_id="src")
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.summary = "other"  # type: ignore[misc]


def test_schedule_event_uses_slots() -> None:
    event = ScheduleEvent(uid="u", summary="s", start=T0, end=T1, source_id="src")
    assert not hasattr(event, "__dict__")


def test_schedule_result_rejects_bad_status() -> None:
    with pytest.raises(ValueError, match="status"):
        ScheduleResult(status="weird", events=(), fetched_at=T0)  # type: ignore[arg-type]


def test_schedule_result_rejects_naive_fetched_at() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ScheduleResult(status="ok", events=(), fetched_at=datetime(2026, 10, 1))


def test_margins_reject_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        Margins(arrival=-1, departure=0, boarding=0, parking_and_walk=0, min_transfer=0)


def test_margins_allow_zero() -> None:
    margins = Margins(
        arrival=0, departure=0, boarding=0, parking_and_walk=0, min_transfer=0
    )
    assert margins.arrival == 0


def _profile(**overrides: object) -> ProfileConfig:
    base: dict[str, object] = dict(
        id="kid_a",
        name="Kid A",
        source_type="ics",
        calendar_entity_id=None,
        source_filter=SourceFilter(),
        destination_id="school",
        dest_lat=59.0,
        dest_lon=18.0,
        default_mode="public_transport",
        static_minutes=None,
        static_label=None,
        weather_adjust=False,
        car_fallback_minutes=None,
        margins=_margins(),
        weekday_mask=frozenset({0, 1, 2, 3, 4}),
        packing_rules=(),
        person_entity_id=None,
        notifications_enabled=True,
        change_threshold_minutes=5,
        quiet_start=time(21, 0),
        quiet_end=time(6, 0),
    )
    base.update(overrides)
    return ProfileConfig(**base)  # type: ignore[arg-type]


def test_profile_rejects_bad_mode() -> None:
    with pytest.raises(ValueError, match="default_mode"):
        _profile(default_mode="plane")


def test_profile_rejects_bad_weekday() -> None:
    with pytest.raises(ValueError, match="weekday_mask"):
        _profile(weekday_mask=frozenset({7}))


def test_profile_rejects_negative_static_minutes() -> None:
    with pytest.raises(ValueError, match="static_minutes"):
        _profile(static_minutes=-10)


def test_profile_defaults_empty_scripts() -> None:
    assert _profile().scripts == {}


def test_day_override_rejects_bad_attendance() -> None:
    with pytest.raises(ValueError, match="attendance"):
        DayOverride(
            person_id="kid_a",
            local_date=date(2026, 10, 1),
            attendance="holiday",  # type: ignore[arg-type]
        )


def test_arrival_requirement_rejects_naive_deadline() -> None:
    with pytest.raises(ValueError, match="arrival_deadline"):
        ArrivalRequirement(
            mission_id="m",
            person_id="kid_a",
            local_date=date(2026, 10, 1),
            event_id="e1",
            event_start=T1,
            arrival_deadline=datetime(2026, 10, 1, 6, 15),
            destination_id="school",
            source="ics",
        )


def test_leg_rejects_bad_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        Leg(kind="drive")  # type: ignore[arg-type]


def test_leg_rejects_naive_times() -> None:
    with pytest.raises(ValueError, match="planned_departure"):
        Leg(kind="transit", planned_departure=datetime(2026, 10, 1, 7, 47))


def test_journey_result_requires_aware_fetched_at() -> None:
    with pytest.raises(ValueError, match="fetched_at"):
        JourneyResult(status="ok", journeys=(), fetched_at=datetime(2026, 10, 1))


def test_duration_result_rejects_negative_minutes() -> None:
    with pytest.raises(ValueError, match="minutes"):
        DurationResult(minutes=-1.0, fetched_at=T0, source="waze", quality="realtime")


def test_duration_result_allows_none_minutes() -> None:
    result = DurationResult(
        minutes=None, fetched_at=T0, source="fallback", quality="unavailable"
    )
    assert result.minutes is None


def test_duration_result_rejects_bad_quality() -> None:
    with pytest.raises(ValueError, match="quality"):
        DurationResult(
            minutes=10.0,
            fetched_at=T0,
            source="waze",
            quality="guessed",  # type: ignore[arg-type]
        )


def test_margin_breakdown_rejects_negative() -> None:
    with pytest.raises(ValueError, match="travel"):
        MarginBreakdown(
            travel=-1, access_walk=0, boarding=0, departure=0, arrival=0, extra_after=0
        )


def test_departure_plan_rejects_bad_status() -> None:
    with pytest.raises(ValueError, match="status"):
        DeparturePlan(
            mission_id="m",
            plan_id="m",
            revision=1,
            requirement=_requirement(),
            mode="car",
            recommended_leave=T0,
            latest_leave=T0,
            last_on_time_alternative_leave=None,
            predicted_arrival=T1,
            journey_id=None,
            route_summary=None,
            quality="estimated",
            feasible=True,
            status="flying",  # type: ignore[arg-type]
            breakdown=None,
            reason_codes=(),
            config_revision=1,
        )


def test_departure_plan_happy_path() -> None:
    plan = DeparturePlan(
        mission_id="m",
        plan_id="m",
        revision=1,
        requirement=_requirement(),
        mode="car",
        recommended_leave=T0,
        latest_leave=T0,
        last_on_time_alternative_leave=None,
        predicted_arrival=T1,
        journey_id=None,
        route_summary="Bil",
        quality="estimated",
        feasible=True,
        status="scheduled",
        breakdown=None,
        reason_codes=("example",),
        config_revision=1,
    )
    assert plan.feasible is True
    assert plan.reason_codes == ("example",)


def test_mission_state_rejects_naive_departed_at() -> None:
    with pytest.raises(ValueError, match="departed_at"):
        MissionState(
            mission_id="m",
            status="departed",
            departed_at=datetime(2026, 10, 1, 6, 0),
            reopened=False,
            notified={},
            first_published_leave=None,
            action_nonce="n",
        )
