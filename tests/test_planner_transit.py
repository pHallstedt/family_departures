"""Tests for the public-transport planner (spec §6.1, §6.2, §7).

These assert product behaviour with controlled time and synthetic journeys:
the §7 worked example (08:20 -> 07:37 / 07:32), candidate filtering against the
minimum transfer margin and reachability, selection of the latest safe home
departure, journey stickiness, the separate ``last_on_time_alternative_leave``,
and the §6.2 disruption rules (cancelled leg, missed transfer, a delayed bus
that must not push the home departure later, and no feasible journey).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.family_departures.models import (
    ArrivalRequirement,
    DeparturePlan,
    Journey,
    JourneyResult,
    Leg,
    Margins,
    MissionState,
    ProfileConfig,
    SourceFilter,
)
from custom_components.family_departures.planner import (
    REASON_KEPT_PREVIOUS_JOURNEY,
    REASON_LATE_ALTERNATIVE_AVAILABLE,
    REASON_NO_JOURNEY,
    REASON_NO_ON_TIME_JOURNEY,
    REASON_REALTIME,
    plan_transit,
)

TZ = ZoneInfo("Europe/Stockholm")
D = date(2026, 10, 1)  # a Thursday


def _local(hh: int, mm: int, d: date = D) -> datetime:
    """Local wall-clock as aware UTC."""
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ).astimezone(UTC)


def _profile(
    *,
    arrival: int = 5,
    departure: int = 5,
    boarding: int = 2,
    min_transfer: int = 5,
) -> ProfileConfig:
    return ProfileConfig(
        id="kid_a",
        name="Kid A",
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
            departure=departure,
            boarding=boarding,
            parking_and_walk=0,
            min_transfer=min_transfer,
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
        mission_id="kid_a:2026-10-01:morning",
        person_id="kid_a",
        local_date=D,
        event_id="evt-1",
        event_start=event_start,
        arrival_deadline=event_start - timedelta(minutes=arrival),
        destination_id="school",
        source="kid_a_ics",
    )


def _walk(dep: datetime, arr: datetime) -> Leg:
    return Leg(
        kind="walk",
        planned_departure=dep,
        planned_arrival=arr,
    )


def _bus(
    *,
    line: str,
    dep: datetime,
    arr: datetime,
    est_dep: datetime | None = None,
    est_arr: datetime | None = None,
    cancelled: bool = False,
    from_stop: str = "SL:1",
    to_stop: str = "SL:2",
) -> Leg:
    return Leg(
        kind="transit",
        line=line,
        direction="City",
        from_stop=from_stop,
        to_stop=to_stop,
        planned_departure=dep,
        estimated_departure=est_dep,
        planned_arrival=arr,
        estimated_arrival=est_arr,
        cancelled=cancelled,
    )


def _journey(journey_id: str, legs: tuple[Leg, ...]) -> Journey:
    return Journey(journey_id=journey_id, legs=legs)


def _result(*journeys: Journey, has_realtime: bool = False) -> JourneyResult:
    return JourneyResult(
        status="ok" if journeys else "empty",
        journeys=journeys,
        fetched_at=_local(6, 0),
        has_realtime=has_realtime,
    )


def _state(status: str = "scheduled") -> MissionState:
    return MissionState(
        mission_id="kid_a:2026-10-01:morning",
        status=status,  # type: ignore[arg-type]
        departed_at=None,
        reopened=False,
        notified={},
        first_published_leave=None,
        action_nonce="n",
    )


# --- §7 worked example: 08:20 -> 07:37 / 07:32 -----------------------------


def test_worked_example_single_bus() -> None:
    # Lesson 08:20, arrival margin 5 -> deadline 08:15.
    # Walk home 8 min (07:39 -> 07:47), bus 07:47 arriving 08:10,
    # boarding margin 2, departure buffer 5.
    # latest = 07:47 - 8 - 2 = 07:37; recommended = 07:37 - 5 = 07:32.
    journey = _journey(
        "j1",
        (
            _walk(_local(7, 39), _local(7, 47)),
            _bus(line="570", dep=_local(7, 47), arr=_local(8, 10)),
        ),
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(journey),
        profile=_profile(arrival=5, departure=5, boarding=2),
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.latest_leave == _local(7, 37)
    assert plan.recommended_leave == _local(7, 32)
    assert plan.predicted_arrival == _local(8, 10)
    assert plan.feasible is True
    assert plan.mode == "public_transport"
    assert plan.journey_id == "j1"
    assert plan.breakdown is not None
    assert plan.breakdown.travel == 23  # 07:47 -> 08:10
    assert plan.breakdown.access_walk == 8
    assert plan.breakdown.boarding == 2
    assert plan.breakdown.departure == 5
    assert plan.breakdown.arrival == 5
    assert plan.breakdown.extra_after == 0


# --- selection: latest safe departure, fewer transfers on near-ties --------


def test_selects_latest_safe_home_departure() -> None:
    early = _journey(
        "early",
        (
            _walk(_local(7, 0), _local(7, 8)),
            _bus(line="A", dep=_local(7, 8), arr=_local(7, 55)),
        ),
    )
    late = _journey(
        "late",
        (
            _walk(_local(7, 39), _local(7, 47)),
            _bus(line="B", dep=_local(7, 47), arr=_local(8, 10)),
        ),
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(early, late),
        profile=_profile(),
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    # The later journey allows leaving home later and still arrives on time.
    assert plan.journey_id == "late"
    assert plan.latest_leave == _local(7, 37)


def test_tie_break_prefers_fewer_transfers() -> None:
    # Two journeys with the same home-departure time; one has a transfer.
    direct = _journey(
        "direct",
        (
            _walk(_local(7, 39), _local(7, 47)),
            _bus(line="D", dep=_local(7, 47), arr=_local(8, 10)),
        ),
    )
    with_change = _journey(
        "change",
        (
            _walk(_local(7, 39), _local(7, 47)),
            _bus(line="C1", dep=_local(7, 47), arr=_local(7, 58), to_stop="SL:9"),
            _bus(
                line="C2",
                dep=_local(8, 5),
                arr=_local(8, 12),
                from_stop="SL:9",
                to_stop="SL:2",
            ),
        ),
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(direct, with_change),
        profile=_profile(min_transfer=5),
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.journey_id == "direct"


# --- §6.2: minimum transfer margin -----------------------------------------


def test_transfer_below_min_transfer_rejected() -> None:
    # Only journey has a 3-minute transfer but min_transfer is 5 -> no on-time.
    tight = _journey(
        "tight",
        (
            _walk(_local(7, 20), _local(7, 28)),
            _bus(line="C1", dep=_local(7, 28), arr=_local(7, 55), to_stop="SL:9"),
            _bus(
                line="C2",
                dep=_local(7, 58),
                arr=_local(8, 10),
                from_stop="SL:9",
                to_stop="SL:2",
            ),
        ),
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(tight),
        profile=_profile(min_transfer=5),
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.feasible is False
    assert plan.status == "cannot_arrive_on_time"
    assert REASON_NO_ON_TIME_JOURNEY in plan.reason_codes


# --- §6.2: first boarding must be reachable from now -----------------------


def test_unreachable_first_boarding_not_proposed() -> None:
    # Bus leaves 06:05, needs 8 min walk + 2 min boarding -> must leave by
    # 05:55, but now is 06:00, so this earlier bus is unreachable and dropped.
    missed = _journey(
        "missed",
        (
            _walk(_local(5, 57), _local(6, 5)),
            _bus(line="E", dep=_local(6, 5), arr=_local(7, 0)),
        ),
    )
    reachable = _journey(
        "reachable",
        (
            _walk(_local(7, 39), _local(7, 47)),
            _bus(line="L", dep=_local(7, 47), arr=_local(8, 10)),
        ),
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(missed, reachable),
        profile=_profile(),
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.journey_id == "reachable"


# --- §6.2: cancelled leg invalidates the journey ---------------------------


def test_cancelled_leg_rejected_and_fallback_used() -> None:
    cancelled = _journey(
        "cancelled",
        (
            _walk(_local(7, 39), _local(7, 47)),
            _bus(line="X", dep=_local(7, 47), arr=_local(8, 10), cancelled=True),
        ),
    )
    alt = _journey(
        "alt",
        (
            _walk(_local(7, 29), _local(7, 37)),
            _bus(line="Y", dep=_local(7, 37), arr=_local(8, 5)),
        ),
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(cancelled, alt),
        profile=_profile(),
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    # The cancelled journey is dropped; the reachable alternative is chosen.
    assert plan.journey_id == "alt"
    assert plan.feasible is True


# --- §6.2: a delayed bus must not push the home departure later ------------


def test_delayed_bus_does_not_push_departure_later() -> None:
    # Bus planned 07:47, now estimated 07:52 (5 min late) but still arrives in
    # time at 08:14. Home departure stays based on the planned 07:47 time.
    on_time = _journey(
        "j",
        (
            _walk(_local(7, 39), _local(7, 47)),
            _bus(
                line="570",
                dep=_local(7, 47),
                arr=_local(8, 9),
                est_dep=_local(7, 52),
                est_arr=_local(8, 14),
            ),
        ),
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(on_time, has_realtime=True),
        profile=_profile(boarding=2),
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    # latest = min(07:47, 07:52) - 8 - 2 = 07:37, unchanged by the delay.
    assert plan.latest_leave == _local(7, 37)
    # Arrival is judged against the realtime estimate.
    assert plan.predicted_arrival == _local(8, 14)
    assert plan.quality == "realtime"
    assert REASON_REALTIME in plan.reason_codes


def test_missed_transfer_from_delay_invalidates_journey() -> None:
    # First leg delayed so it arrives after the second leg has left minus the
    # transfer margin: the journey is no longer usable.
    broken = _journey(
        "broken",
        (
            _walk(_local(7, 20), _local(7, 28)),
            _bus(
                line="C1",
                dep=_local(7, 28),
                arr=_local(7, 55),
                est_arr=_local(8, 2),  # 7 min late
                to_stop="SL:9",
            ),
            _bus(
                line="C2",
                dep=_local(8, 5),
                arr=_local(8, 12),
                from_stop="SL:9",
                to_stop="SL:2",
            ),
        ),
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(broken, has_realtime=True),
        profile=_profile(min_transfer=5),  # 8:02 -> 8:05 is only 3 min
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.feasible is False
    assert plan.status == "cannot_arrive_on_time"


# --- §6.2: no journey can arrive on time -----------------------------------


def test_no_on_time_journey_shows_best_late() -> None:
    late = _journey(
        "late",
        (
            _walk(_local(8, 0), _local(8, 8)),
            _bus(line="Z", dep=_local(8, 8), arr=_local(8, 40)),
        ),
    )
    later = _journey(
        "later",
        (
            _walk(_local(8, 20), _local(8, 28)),
            _bus(line="Z2", dep=_local(8, 28), arr=_local(9, 0)),
        ),
    )
    plan = plan_transit(
        req=_requirement(),  # deadline 08:15
        result=_result(late, later),
        profile=_profile(),
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.feasible is False
    assert plan.status == "cannot_arrive_on_time"
    # The least-late option (arrives 08:40) is offered, not the 09:00 one.
    assert plan.journey_id == "late"
    assert plan.predicted_arrival == _local(8, 40)
    assert plan.last_on_time_alternative_leave is None


def test_empty_result_cannot_arrive() -> None:
    plan = plan_transit(
        req=_requirement(),
        result=_result(),
        profile=_profile(),
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.status == "cannot_arrive_on_time"
    assert plan.recommended_leave is None
    assert plan.quality == "unavailable"
    assert REASON_NO_ON_TIME_JOURNEY in plan.reason_codes


def test_error_result_marks_no_journey_data() -> None:
    err = JourneyResult(
        status="error",
        journeys=(),
        fetched_at=_local(6, 0),
        error_code="timeout",
    )
    plan = plan_transit(
        req=_requirement(),
        result=err,
        profile=_profile(),
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.status == "cannot_arrive_on_time"
    assert plan.quality == "unavailable"
    assert REASON_NO_JOURNEY in plan.reason_codes


# --- stickiness and the separate late alternative --------------------------


def test_sticks_to_previous_journey_on_small_improvement() -> None:
    # Previously chose "slow" (leave 07:35). A new "fast" lets us leave 07:36,
    # only 1 min better -> keep the previous journey (no jumpy advice).
    slow = _journey(
        "slow",
        (
            _walk(_local(7, 37), _local(7, 45)),
            _bus(line="S", dep=_local(7, 45), arr=_local(8, 12)),
        ),
    )
    fast = _journey(
        "fast",
        (
            _walk(_local(7, 38), _local(7, 46)),
            _bus(line="F", dep=_local(7, 46), arr=_local(8, 10)),
        ),
    )
    previous = DeparturePlan(
        mission_id="kid_a:2026-10-01:morning",
        plan_id="kid_a:2026-10-01:morning",
        revision=1,
        requirement=_requirement(),
        mode="public_transport",
        recommended_leave=_local(7, 30),
        latest_leave=_local(7, 35),
        last_on_time_alternative_leave=None,
        predicted_arrival=_local(8, 12),
        journey_id="slow",
        route_summary="S",
        quality="scheduled",
        feasible=True,
        status="scheduled",
        breakdown=None,
        reason_codes=(),
        config_revision=0,
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(slow, fast),
        profile=_profile(boarding=2),
        state=_state(),
        previous=previous,
        now=_local(6, 0),
        revision=2,
    )
    assert plan.journey_id == "slow"
    assert REASON_KEPT_PREVIOUS_JOURNEY in plan.reason_codes


def test_switches_journey_on_large_improvement() -> None:
    # The new journey lets us leave more than 2 minutes later -> switch.
    slow = _journey(
        "slow",
        (
            _walk(_local(7, 20), _local(7, 28)),
            _bus(line="S", dep=_local(7, 28), arr=_local(8, 10)),
        ),
    )
    fast = _journey(
        "fast",
        (
            _walk(_local(7, 39), _local(7, 47)),
            _bus(line="F", dep=_local(7, 47), arr=_local(8, 12)),
        ),
    )
    previous = DeparturePlan(
        mission_id="kid_a:2026-10-01:morning",
        plan_id="kid_a:2026-10-01:morning",
        revision=1,
        requirement=_requirement(),
        mode="public_transport",
        recommended_leave=_local(7, 13),
        latest_leave=_local(7, 18),
        last_on_time_alternative_leave=None,
        predicted_arrival=_local(8, 10),
        journey_id="slow",
        route_summary="S",
        quality="scheduled",
        feasible=True,
        status="scheduled",
        breakdown=None,
        reason_codes=(),
        config_revision=0,
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(slow, fast),
        profile=_profile(boarding=2),
        state=_state(),
        previous=previous,
        now=_local(6, 0),
        revision=2,
    )
    assert plan.journey_id == "fast"
    assert REASON_KEPT_PREVIOUS_JOURNEY not in plan.reason_codes


def test_latest_departing_selected_has_no_alternative() -> None:
    # When the selected journey is itself the latest-departing feasible one,
    # there is no even-later on-time journey, so no separate alternative.
    early = _journey(
        "early",
        (
            _walk(_local(7, 20), _local(7, 28)),
            _bus(line="A", dep=_local(7, 28), arr=_local(8, 0)),
        ),
    )
    later = _journey(
        "later",
        (
            _walk(_local(7, 39), _local(7, 47)),
            _bus(line="B", dep=_local(7, 47), arr=_local(8, 12)),
        ),
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(early, later),
        profile=_profile(boarding=2),
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.journey_id == "later"
    assert plan.latest_leave == _local(7, 37)
    assert plan.last_on_time_alternative_leave is None
    assert REASON_LATE_ALTERNATIVE_AVAILABLE not in plan.reason_codes


def test_late_alternative_exposed_when_sticky_keeps_earlier() -> None:
    # Previously chose "slow" (leave 07:35). A "fast" lets us leave 07:36, only
    # 1 min better, so stickiness keeps "slow". The 1-min-later "fast" journey
    # still meets the deadline, so it is surfaced as
    # last_on_time_alternative_leave, kept separate from latest_leave (spec §7).
    slow = _journey(
        "slow",
        (
            _walk(_local(7, 37), _local(7, 45)),
            _bus(line="S", dep=_local(7, 45), arr=_local(8, 12)),
        ),
    )
    fast = _journey(
        "fast",
        (
            _walk(_local(7, 38), _local(7, 46)),
            _bus(line="F", dep=_local(7, 46), arr=_local(8, 10)),
        ),
    )
    previous = DeparturePlan(
        mission_id="kid_a:2026-10-01:morning",
        plan_id="kid_a:2026-10-01:morning",
        revision=1,
        requirement=_requirement(),
        mode="public_transport",
        recommended_leave=_local(7, 30),
        latest_leave=_local(7, 35),
        last_on_time_alternative_leave=None,
        predicted_arrival=_local(8, 12),
        journey_id="slow",
        route_summary="S",
        quality="scheduled",
        feasible=True,
        status="scheduled",
        breakdown=None,
        reason_codes=(),
        config_revision=0,
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(slow, fast),
        profile=_profile(boarding=2),
        state=_state(),
        previous=previous,
        now=_local(6, 0),
        revision=2,
    )
    # Kept "slow": latest_leave = 07:45 - 8 - 2 = 07:35.
    assert plan.journey_id == "slow"
    assert plan.latest_leave == _local(7, 35)
    assert REASON_KEPT_PREVIOUS_JOURNEY in plan.reason_codes
    # "fast" leaves home at 07:46 - 8 - 2 = 07:36, 1 min later than selected.
    assert plan.last_on_time_alternative_leave == _local(7, 36)
    assert REASON_LATE_ALTERNATIVE_AVAILABLE in plan.reason_codes


def test_final_walk_counts_towards_arrival() -> None:
    # A trailing walk leg (bus stop -> school door) is part of arrival: the
    # bus arrives 08:12 but the 5-minute walk lands at 08:17, past the 08:15
    # deadline, so the journey is not feasible.
    journey = _journey(
        "j",
        (
            _walk(_local(7, 39), _local(7, 47)),
            _bus(line="570", dep=_local(7, 47), arr=_local(8, 12)),
            _walk(_local(8, 12), _local(8, 17)),
        ),
    )
    plan = plan_transit(
        req=_requirement(),  # deadline 08:15
        result=_result(journey),
        profile=_profile(),
        state=None,
        previous=None,
        now=_local(6, 0),
        revision=1,
    )
    assert plan.predicted_arrival == _local(8, 17)
    assert plan.feasible is False
    assert plan.status == "cannot_arrive_on_time"


def test_departed_state_wins_over_clock_in_transit() -> None:
    journey = _journey(
        "j1",
        (
            _walk(_local(7, 39), _local(7, 47)),
            _bus(line="570", dep=_local(7, 47), arr=_local(8, 10)),
        ),
    )
    plan = plan_transit(
        req=_requirement(),
        result=_result(journey),
        profile=_profile(),
        state=_state("departed"),
        previous=None,
        now=_local(7, 36),  # would be leave_now otherwise
        revision=1,
    )
    assert plan.status == "departed"
