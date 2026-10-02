"""Tests for the car/static planner (spec §6.3, §6.4, §7).

These assert product behaviour with controlled time and synthetic data: the
worked §7 formulas with exact minutes, the car-only parking/walk margin, the
weather-surcharge rule (§6.4), the fallback/quality markings and the
"never zero travel from missing data" rule (§6.3, §11.2).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.family_departures.models import (
    ArrivalRequirement,
    DurationResult,
    Margins,
    MissionState,
    ProfileConfig,
    SourceFilter,
)
from custom_components.family_departures.planner import (
    RAIN_SURCHARGE_MINUTES,
    REASON_FALLBACK_TRAVEL_TIME,
    REASON_NO_TRAVEL_TIME,
    REASON_STALE_TRAVEL_TIME,
    REASON_STATIC_TRAVEL_TIME,
    SNOW_ICE_SURCHARGE_MINUTES,
    plan_fixed,
    weather_surcharge_minutes,
)

TZ = ZoneInfo("Europe/Stockholm")
D = date(2026, 10, 1)  # a Thursday


def _local(hh: int, mm: int, d: date = D) -> datetime:
    """Local wall-clock as aware UTC."""
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ).astimezone(UTC)


def _profile(
    *,
    mode: str = "car",
    arrival: int = 5,
    departure: int = 5,
    parking_and_walk: int = 5,
    static_minutes: int | None = None,
    static_label: str | None = None,
    weather_adjust: bool = False,
) -> ProfileConfig:
    return ProfileConfig(
        id="parent_b",
        name="Parent B",
        source_type="ha_calendar",
        calendar_entity_id="calendar.parent_b",
        source_filter=SourceFilter(),
        destination_id="work",
        dest_lat=59.3,
        dest_lon=18.0,
        default_mode=mode,  # type: ignore[arg-type]
        static_minutes=static_minutes,
        static_label=static_label,
        weather_adjust=weather_adjust,
        car_fallback_minutes=35,
        margins=Margins(
            arrival=arrival,
            departure=departure,
            boarding=0,
            parking_and_walk=parking_and_walk,
            min_transfer=0,
        ),
        weekday_mask=frozenset({0, 1, 2, 3, 4}),
        packing_rules=(),
        person_entity_id=None,
        notifications_enabled=True,
        change_threshold_minutes=5,
        quiet_start=time(21, 0),
        quiet_end=time(6, 0),
        scripts={},
    )


def _requirement(
    *, event_hh: int = 8, event_mm: int = 20, arrival: int = 5
) -> ArrivalRequirement:
    event_start = _local(event_hh, event_mm)
    return ArrivalRequirement(
        mission_id="parent_b:2026-10-01:morning",
        person_id="parent_b",
        local_date=D,
        event_id="evt-1",
        event_start=event_start,
        arrival_deadline=event_start - timedelta(minutes=arrival),
        destination_id="work",
        source="parent_b_cal",
    )


def _duration(
    minutes: float | None,
    *,
    source: str = "waze",
    quality: str = "realtime",
    route_name: str | None = "E4",
) -> DurationResult:
    return DurationResult(
        minutes=minutes,
        fetched_at=_local(5, 0),
        source=source,  # type: ignore[arg-type]
        quality=quality,  # type: ignore[arg-type]
        route_name=route_name,
    )


def _state(status: str) -> MissionState:
    return MissionState(
        mission_id="parent_b:2026-10-01:morning",
        status=status,  # type: ignore[arg-type]
        departed_at=_local(7, 0) if status == "departed" else None,
        reopened=False,
        notified={},
        first_published_leave=None,
        action_nonce="n",
    )


# --- §7 worked example: car ------------------------------------------------


def test_car_worked_example_exact_minutes() -> None:
    # Lesson/arrival 08:20, arrival margin 5 -> deadline 08:15.
    # Travel 25, parking/walk 5, departure buffer 5.
    plan = plan_fixed(
        req=_requirement(),
        mode="car",
        duration=_duration(25),
        profile=_profile(mode="car", arrival=5, departure=5, parking_and_walk=5),
        state=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.requirement.arrival_deadline == _local(8, 15)
    assert plan.latest_leave == _local(7, 45)  # 08:15 - 25 - 5
    assert plan.recommended_leave == _local(7, 40)  # 07:45 - 5
    assert plan.predicted_arrival == _local(8, 10)  # 07:40 + 25 + 5
    assert plan.feasible is True
    assert plan.mode == "car"
    assert plan.quality == "realtime"
    assert plan.route_summary == "E4"


def test_car_breakdown_stacks_margins() -> None:
    plan = plan_fixed(
        req=_requirement(),
        mode="car",
        duration=_duration(25),
        profile=_profile(mode="car", arrival=5, departure=5, parking_and_walk=5),
        state=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.breakdown is not None
    assert plan.breakdown.travel == 25
    assert plan.breakdown.extra_after == 5  # parking/walk for car
    assert plan.breakdown.departure == 5
    assert plan.breakdown.arrival == 5
    assert plan.breakdown.access_walk == 0
    assert plan.breakdown.boarding == 0


# --- §7 worked example: static (bike) --------------------------------------


def test_static_worked_example_no_parking_margin() -> None:
    # Static door-to-door 20 min; extra_after_travel is 0 for static.
    plan = plan_fixed(
        req=_requirement(),
        mode="static",
        duration=_duration(20, source="static", quality="scheduled", route_name=None),
        profile=_profile(
            mode="static",
            arrival=5,
            departure=5,
            parking_and_walk=5,
            static_minutes=20,
            static_label="Cykel",
        ),
        state=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.latest_leave == _local(7, 55)  # 08:15 - 20 - 0
    assert plan.recommended_leave == _local(7, 50)  # 07:55 - 5
    assert plan.predicted_arrival == _local(8, 10)  # 07:50 + 20
    assert plan.breakdown is not None
    assert plan.breakdown.extra_after == 0  # parking/walk never added for static
    assert plan.quality == "scheduled"
    assert REASON_STATIC_TRAVEL_TIME in plan.reason_codes


def test_parking_and_walk_added_for_car_only() -> None:
    common = dict(
        req=_requirement(),
        duration=_duration(20, source="static", quality="scheduled"),
        state=None,
        now=_local(6, 0),
        revision=1,
    )
    car = plan_fixed(
        mode="car",
        profile=_profile(mode="car", departure=0, parking_and_walk=8),
        **common,  # type: ignore[arg-type]
    )
    static = plan_fixed(
        mode="static",
        profile=_profile(mode="static", departure=0, parking_and_walk=8),
        **common,  # type: ignore[arg-type]
    )
    # Same deadline and travel; the car leaves 8 minutes earlier for parking.
    assert static.latest_leave is not None
    assert car.latest_leave is not None
    assert static.latest_leave - car.latest_leave == timedelta(minutes=8)


# --- §6.3/§11.2: missing data never becomes a zero-minute trip -------------


def test_missing_duration_without_fallback_needs_configuration() -> None:
    plan = plan_fixed(
        req=_requirement(),
        mode="car",
        duration=_duration(None, source="fallback", quality="unavailable"),
        profile=_profile(mode="car"),
        state=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.status == "needs_configuration"
    assert plan.feasible is False
    assert plan.recommended_leave is None
    assert plan.latest_leave is None
    assert plan.predicted_arrival is None
    assert plan.breakdown is None
    assert plan.quality == "unavailable"
    assert REASON_NO_TRAVEL_TIME in plan.reason_codes


def test_fallback_duration_marked_estimated() -> None:
    plan = plan_fixed(
        req=_requirement(),
        mode="car",
        duration=_duration(35, source="fallback", quality="estimated"),
        profile=_profile(mode="car"),
        state=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.feasible is True
    assert plan.quality == "estimated"
    assert REASON_FALLBACK_TRAVEL_TIME in plan.reason_codes


def test_stale_duration_marked_stale() -> None:
    plan = plan_fixed(
        req=_requirement(),
        mode="car",
        duration=_duration(25, source="waze", quality="stale"),
        profile=_profile(mode="car"),
        state=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.quality == "stale"
    assert REASON_STALE_TRAVEL_TIME in plan.reason_codes


# --- status derivation from now (spec §10) ---------------------------------


def test_status_scheduled_more_than_hour_before() -> None:
    plan = plan_fixed(
        req=_requirement(),
        mode="car",
        duration=_duration(25),
        profile=_profile(mode="car", departure=5, parking_and_walk=5),
        state=None,
        now=_local(6, 0),  # recommended is 07:40, >60 min away
        revision=1,
    )
    assert plan.status == "scheduled"


def test_status_preparing_within_hour() -> None:
    plan = plan_fixed(
        req=_requirement(),
        mode="car",
        duration=_duration(25),
        profile=_profile(mode="car", departure=5, parking_and_walk=5),
        state=None,
        now=_local(7, 0),  # recommended 07:40, 40 min away
        revision=1,
    )
    assert plan.status == "preparing"


def test_status_leave_now_between_recommended_and_latest() -> None:
    plan = plan_fixed(
        req=_requirement(),
        mode="car",
        duration=_duration(25),
        profile=_profile(mode="car", departure=5, parking_and_walk=5),
        state=None,
        now=_local(7, 42),  # recommended 07:40, latest 07:45
        revision=1,
    )
    assert plan.status == "leave_now"


def test_status_late_past_latest_leave() -> None:
    plan = plan_fixed(
        req=_requirement(),
        mode="car",
        duration=_duration(25),
        profile=_profile(mode="car", departure=5, parking_and_walk=5),
        state=None,
        now=_local(7, 50),  # latest 07:45 already passed
        revision=1,
    )
    assert plan.status == "late"


def test_departed_state_wins_over_clock() -> None:
    plan = plan_fixed(
        req=_requirement(),
        mode="car",
        duration=_duration(25),
        profile=_profile(mode="car", departure=5, parking_and_walk=5),
        state=_state("departed"),
        now=_local(7, 42),  # would otherwise be leave_now
        revision=1,
    )
    assert plan.status == "departed"


# --- §6.4 weather surcharge rule -------------------------------------------


def test_weather_surcharge_disabled_is_zero() -> None:
    assert weather_surcharge_minutes(enabled=False, rain=True, snow_or_ice=True) == 0


def test_weather_surcharge_takes_max_by_default() -> None:
    assert (
        weather_surcharge_minutes(enabled=True, rain=True, snow_or_ice=True)
        == SNOW_ICE_SURCHARGE_MINUTES
    )
    assert weather_surcharge_minutes(enabled=True, rain=True) == RAIN_SURCHARGE_MINUTES


def test_weather_surcharge_additive_when_explicit() -> None:
    assert (
        weather_surcharge_minutes(
            enabled=True, rain=True, snow_or_ice=True, additive=True
        )
        == RAIN_SURCHARGE_MINUTES + SNOW_ICE_SURCHARGE_MINUTES
    )


def test_weather_surcharge_missing_data_is_zero() -> None:
    # No active conditions passed (missing data) is reported as zero surcharge,
    # never silently treated as bad weather.
    assert weather_surcharge_minutes(enabled=True) == 0


def test_weather_surcharge_baked_into_duration_shifts_departure() -> None:
    # A caller that applies a +5 rain surcharge to the static duration pushes
    # the home departure 5 minutes earlier; the planner reads it from minutes.
    base = plan_fixed(
        req=_requirement(),
        mode="static",
        duration=_duration(20, source="static", quality="scheduled"),
        profile=_profile(mode="static", departure=0),
        state=None,
        now=_local(6, 0),
        revision=1,
    )
    surcharge = weather_surcharge_minutes(enabled=True, rain=True)
    wet = plan_fixed(
        req=_requirement(),
        mode="static",
        duration=_duration(20 + surcharge, source="static", quality="scheduled"),
        profile=_profile(mode="static", departure=0),
        state=None,
        now=_local(6, 0),
        revision=1,
    )
    assert base.latest_leave is not None
    assert wet.latest_leave is not None
    assert base.latest_leave - wet.latest_leave == timedelta(minutes=surcharge)
