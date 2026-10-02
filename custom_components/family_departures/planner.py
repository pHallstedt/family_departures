"""Departure planning for car and static modes (spec §6.3, §6.4, §7).

This module turns an :class:`ArrivalRequirement` plus a travel duration into a
:class:`DeparturePlan`: the recommended and latest home-departure times, the
predicted arrival, the stacked margin breakdown, the data quality and the
mission status.

It is pure (plan §1 rule 3): no ``homeassistant`` import, ``now`` is a
parameter and the clock is never read here. All datetimes are aware UTC.

Formulas (spec §7), for ``car`` and ``static``::

    latest_leave      = arrival_deadline − travel_duration − extra_after_travel
    recommended_leave = latest_leave − departure_buffer

``extra_after_travel`` is the parking-and-walk margin for ``car`` and zero for
``static`` (the static duration is door-to-door and already includes any
parking/locking, §7). Parking and walking are therefore added for car only and
never inside the travel duration. The weather surcharge, when enabled, is baked
into the travel duration upstream (§6.4, §7: "Väderpåslag ingår i
travel_duration"); :func:`weather_surcharge_minutes` computes it so the
coordinator/static provider can apply it before calling :func:`plan_fixed`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import (
    ArrivalRequirement,
    DeparturePlan,
    DurationResult,
    Journey,
    JourneyResult,
    Leg,
    MarginBreakdown,
    MissionState,
    MissionStatus,
    Mode,
    ProfileConfig,
    Quality,
)

# Default weather surcharges in minutes (spec §6.4). Adjustable start values,
# not meteorologically validated models.
RAIN_SURCHARGE_MINUTES = 5
SNOW_ICE_SURCHARGE_MINUTES = 10

# The staleness guard (`config_revision`, spec §11.2) is owned by the
# coordinator (T13); the pure planner has no access to it and carries this
# stable default, which the coordinator overrides when it builds the plan.
_DEFAULT_CONFIG_REVISION = 0

# How long before the recommended departure the mission enters "preparing"
# (status only; the morning notification is still driven separately at −60 by
# the notification policy, §12.1).
PREPARE_WINDOW_MINUTES = 60

# Reason codes attached to the plan so the dashboard/diagnostics can explain it
# without re-deriving the logic.
REASON_NO_TRAVEL_TIME = "no_travel_time"
REASON_FALLBACK_TRAVEL_TIME = "fallback_travel_time"
REASON_STATIC_TRAVEL_TIME = "static_travel_time"
REASON_STALE_TRAVEL_TIME = "stale_travel_time"

# Transit-specific reason codes (spec §6.1, §6.2).
REASON_NO_JOURNEY = "no_journey_data"
REASON_NO_ON_TIME_JOURNEY = "no_on_time_journey"
REASON_JOURNEY_CANCELLED = "journey_leg_cancelled"
REASON_LATE_ALTERNATIVE_AVAILABLE = "late_alternative_available"
REASON_KEPT_PREVIOUS_JOURNEY = "kept_previous_journey"
REASON_REALTIME = "realtime"

# A later alternative journey is only exposed/kept over the sticky selection
# when it saves more than this many minutes of home departure (spec §7:
# "Behåll redan vald resa vid små förbättringar").
STICKY_IMPROVEMENT_MINUTES = 2


def weather_surcharge_minutes(
    *,
    enabled: bool,
    rain: bool = False,
    snow_or_ice: bool = False,
    additive: bool = False,
) -> int:
    """Return the weather surcharge in whole minutes (spec §6.4).

    Returns ``0`` unless ``enabled`` (the per-profile ``weather_adjust`` flag).
    By default the highest active surcharge applies, not the sum; pass
    ``additive=True`` only when an explicit additive policy is configured.
    Missing weather data must be passed as ``False`` by the caller: absent data
    is reported as missing and never treated as "good cycling weather".
    """
    if not enabled:
        return 0
    surcharges = []
    if rain:
        surcharges.append(RAIN_SURCHARGE_MINUTES)
    if snow_or_ice:
        surcharges.append(SNOW_ICE_SURCHARGE_MINUTES)
    if not surcharges:
        return 0
    return sum(surcharges) if additive else max(surcharges)


def _derive_status(
    now: datetime,
    recommended_leave: datetime,
    latest_leave: datetime,
    state: MissionState | None,
) -> MissionStatus:
    """Derive the mission status from ``now`` and the computed leave times.

    State-confirmed outcomes (``departed``/``skipped``) win over the clock so a
    confirmed departure is never reset to ``leave_now`` by a late recompute
    (spec §10, §11.2). Otherwise:

    * before ``recommended_leave − 60 min`` → ``scheduled``
    * within the hour before ``recommended_leave`` → ``preparing``
    * from ``recommended_leave`` to ``latest_leave`` → ``leave_now``
    * past ``latest_leave`` → ``late``
    """
    if state is not None and state.status in ("departed", "skipped"):
        return state.status

    if now < recommended_leave - timedelta(minutes=PREPARE_WINDOW_MINUTES):
        return "scheduled"
    if now < recommended_leave:
        return "preparing"
    if now <= latest_leave:
        return "leave_now"
    return "late"


def _quality_for_duration(duration: DurationResult) -> Quality:
    """Map a :class:`DurationResult` to the plan's data quality (spec §11.2)."""
    if duration.quality == "stale":
        return "stale"
    if duration.source == "fallback":
        return "estimated"
    if duration.source == "static":
        return "scheduled"
    # A live Waze duration carries its own quality (realtime/estimated/...).
    return duration.quality


def plan_fixed(
    req: ArrivalRequirement,
    mode: Mode,
    duration: DurationResult,
    profile: ProfileConfig,
    state: MissionState | None,
    now: datetime,
    revision: int,
) -> DeparturePlan:
    """Build a departure plan for a fixed-duration mode (``car`` or ``static``).

    Applies the §7 formulas and derives the status from ``now``. When the
    travel duration is unknown and no fallback is available the plan is marked
    ``needs_configuration`` with no times, so a missing Waze answer never
    produces a zero-minute trip (§6.3, §11.2).
    """
    extra_after_travel = profile.margins.parking_and_walk if mode == "car" else 0
    departure_buffer = profile.margins.departure

    # Unknown travel time with no safe fallback: no departure times, configure.
    if duration.minutes is None:
        return DeparturePlan(
            mission_id=req.mission_id,
            plan_id=req.mission_id,
            revision=revision,
            requirement=req,
            mode=mode,
            recommended_leave=None,
            latest_leave=None,
            last_on_time_alternative_leave=None,
            predicted_arrival=None,
            journey_id=None,
            route_summary=None,
            quality="unavailable",
            feasible=False,
            status="needs_configuration",
            breakdown=None,
            reason_codes=(REASON_NO_TRAVEL_TIME,),
            config_revision=_DEFAULT_CONFIG_REVISION,
        )

    travel_minutes = int(round(duration.minutes))
    latest_leave = (
        req.arrival_deadline
        - timedelta(minutes=travel_minutes)
        - timedelta(minutes=extra_after_travel)
    )
    recommended_leave = latest_leave - timedelta(minutes=departure_buffer)
    # Predicted arrival if you leave at the recommended time: travel plus any
    # extra-after-travel margin (parking/walk for car). The departure buffer is
    # home slack, so leaving at the recommended time arrives the buffer early;
    # leaving at ``latest_leave`` would arrive exactly at the deadline.
    predicted_arrival = recommended_leave + timedelta(
        minutes=travel_minutes + extra_after_travel
    )

    breakdown = MarginBreakdown(
        travel=travel_minutes,
        access_walk=0,
        boarding=0,
        departure=departure_buffer,
        arrival=profile.margins.arrival,
        extra_after=extra_after_travel,
    )

    quality = _quality_for_duration(duration)
    reasons: list[str] = []
    if duration.source == "fallback":
        reasons.append(REASON_FALLBACK_TRAVEL_TIME)
    if duration.source == "static":
        reasons.append(REASON_STATIC_TRAVEL_TIME)
    if duration.quality == "stale":
        reasons.append(REASON_STALE_TRAVEL_TIME)

    status = _derive_status(now, recommended_leave, latest_leave, state)

    return DeparturePlan(
        mission_id=req.mission_id,
        plan_id=req.mission_id,
        revision=revision,
        requirement=req,
        mode=mode,
        recommended_leave=recommended_leave,
        latest_leave=latest_leave,
        last_on_time_alternative_leave=None,
        predicted_arrival=predicted_arrival,
        journey_id=None,
        route_summary=duration.route_name,
        quality=quality,
        feasible=True,
        status=status,
        breakdown=breakdown,
        reason_codes=tuple(reasons),
        config_revision=_DEFAULT_CONFIG_REVISION,
    )


# --- Public transport (spec §6.1, §6.2, §7) --------------------------------


def _effective_departure(leg: Leg) -> datetime | None:
    """Return ``min(planned, estimated)`` departure for a transit leg (§7).

    The spec uses the earlier of the planned and estimated departure as
    ``first_vehicle_departure`` so a bus that is running early is not missed.
    A delayed bus (estimated later than planned) therefore keeps the planned
    time, which is what stops a delay from pushing the home departure later
    (§6.2). Returns ``None`` when the leg has no departure time at all.
    """
    planned = leg.planned_departure
    estimated = leg.estimated_departure
    if planned is None:
        return estimated
    if estimated is None:
        return planned
    return min(planned, estimated)


def _effective_arrival(leg: Leg) -> datetime | None:
    """Return the estimated arrival if present, else the planned arrival.

    Final arrival uses the realtime estimate when available so a late journey
    is judged against its predicted arrival, not an optimistic timetable.
    """
    if leg.estimated_arrival is not None:
        return leg.estimated_arrival
    return leg.planned_arrival


def _transit_legs(journey: Journey) -> tuple[Leg, ...]:
    """Return the transit (non-walk) legs of a journey in order."""
    return tuple(leg for leg in journey.legs if leg.kind == "transit")


def _final_arrival(journey: Journey) -> datetime | None:
    """Arrival at the destination, i.e. the last leg's arrival (§7).

    This includes any trailing walk leg so the arrival is door-to-door and the
    feasibility check against ``arrival_deadline`` is not optimistic. The final
    walk is part of arrival, not a separate margin, and is never subtracted
    again elsewhere.
    """
    for leg in reversed(journey.legs):
        arr = _effective_arrival(leg)
        if arr is not None:
            return arr
    return None


def _access_walk_minutes(journey: Journey) -> int:
    """Minutes of walking before the first transit leg (home access walk, §7).

    This is time already spent travelling, not a safety margin; it is summed
    from the leading walk legs and never double-counted against the boarding
    margin or the in-vehicle time.
    """
    total = timedelta(0)
    for leg in journey.legs:
        if leg.kind == "transit":
            break
        dep = leg.planned_departure
        arr = leg.planned_arrival
        if dep is not None and arr is not None and arr > dep:
            total += arr - dep
    return int(round(total.total_seconds() / 60))


def _journey_has_realtime(journey: Journey) -> bool:
    """True if any leg carries a realtime estimate distinct from the plan."""
    for leg in journey.legs:
        if (
            leg.estimated_departure is not None
            and leg.estimated_departure != leg.planned_departure
        ):
            return True
        if (
            leg.estimated_arrival is not None
            and leg.estimated_arrival != leg.planned_arrival
        ):
            return True
    return False


class _CandidateJourney:
    """A journey evaluated against the requirement (internal to the planner)."""

    __slots__ = (
        "journey",
        "latest_leave",
        "predicted_arrival",
        "transfers",
        "feasible",
    )

    def __init__(
        self,
        journey: Journey,
        latest_leave: datetime,
        predicted_arrival: datetime,
        transfers: int,
        feasible: bool,
    ) -> None:
        self.journey = journey
        self.latest_leave = latest_leave
        self.predicted_arrival = predicted_arrival
        self.transfers = transfers
        self.feasible = feasible


def _evaluate_journey(
    journey: Journey,
    req: ArrivalRequirement,
    profile: ProfileConfig,
    now: datetime,
) -> _CandidateJourney | None:
    """Validate one journey and compute its home-departure times.

    Returns ``None`` when the journey is unusable (cancelled leg, missing
    times, a transfer below ``min_transfer`` or a first boarding that is no
    longer reachable from ``now``). A journey that is well-formed but arrives
    after the deadline is returned with ``feasible=False`` so it can still be
    offered as a late alternative (§6.2).
    """
    transit_legs = _transit_legs(journey)
    if not transit_legs:
        return None

    # A cancelled leg invalidates the whole journey (§6.2).
    if any(leg.cancelled for leg in journey.legs):
        return None

    first_dep = _effective_departure(transit_legs[0])
    final_arr = _final_arrival(journey)
    if first_dep is None or final_arr is None:
        return None

    # Every transfer must still clear the minimum transfer margin (§6.2):
    # time between arriving on one transit leg and departing on the next.
    for prev_leg, next_leg in zip(transit_legs, transit_legs[1:], strict=False):
        prev_arr = _effective_arrival(prev_leg)
        next_dep = _effective_departure(next_leg)
        if prev_arr is None or next_dep is None:
            return None
        gap = (next_dep - prev_arr).total_seconds() / 60
        if gap < profile.margins.min_transfer:
            return None

    access_walk = _access_walk_minutes(journey)
    latest_leave = (
        first_dep
        - timedelta(minutes=access_walk)
        - timedelta(minutes=profile.margins.boarding)
    )

    # The first boarding must still be reachable: leaving now, walking and
    # making the boarding margin must land us at the stop in time. An earlier
    # bus the user can no longer catch must never be proposed (§6.2).
    earliest_reach = now + timedelta(minutes=access_walk + profile.margins.boarding)
    if first_dep < earliest_reach:
        return None

    transfers = max(len(transit_legs) - 1, 0)
    feasible = final_arr <= req.arrival_deadline
    return _CandidateJourney(
        journey=journey,
        latest_leave=latest_leave,
        predicted_arrival=final_arr,
        transfers=transfers,
        feasible=feasible,
    )


def _select_candidate(
    candidates: list[_CandidateJourney],
) -> _CandidateJourney | None:
    """Pick the latest safe home departure, breaking ties on fewer transfers.

    Among feasible candidates we want the latest possible home departure (§7:
    "Välj normalt senaste möjliga hemavgång bland säkra alternativ"); when two
    are near-equivalent, prefer fewer transfers.
    """
    feasible = [c for c in candidates if c.feasible]
    if not feasible:
        return None
    return max(feasible, key=lambda c: (c.latest_leave, -c.transfers))


def _latest_on_time_alternative(
    selected: _CandidateJourney,
    candidates: list[_CandidateJourney],
) -> datetime | None:
    """A later on-time journey's home departure, if one exists (§7).

    ``last_on_time_alternative_leave`` is deliberately separate from
    ``latest_leave`` (which is the selected journey). It is only populated when
    a *different* feasible journey allows leaving home later than the selected
    one, so the UI can show "a later journey still arrives on time" without
    mislabelling it as "latest in time".
    """
    later = [
        c
        for c in candidates
        if c.feasible
        and c.journey.journey_id != selected.journey.journey_id
        and c.latest_leave > selected.latest_leave
    ]
    if not later:
        return None
    return max(c.latest_leave for c in later)


def _best_late(candidates: list[_CandidateJourney]) -> _CandidateJourney | None:
    """The least-late journey when none can arrive on time (§6.2)."""
    if not candidates:
        return None
    return min(candidates, key=lambda c: c.predicted_arrival)


def _no_journey_plan(
    req: ArrivalRequirement,
    state: MissionState | None,
    revision: int,
    reason: str,
) -> DeparturePlan:
    """Plan returned when no usable journey data is available at all."""
    return DeparturePlan(
        mission_id=req.mission_id,
        plan_id=req.mission_id,
        revision=revision,
        requirement=req,
        mode="public_transport",
        recommended_leave=None,
        latest_leave=None,
        last_on_time_alternative_leave=None,
        predicted_arrival=None,
        journey_id=None,
        route_summary=None,
        quality="unavailable",
        feasible=False,
        status="cannot_arrive_on_time",
        breakdown=None,
        reason_codes=(reason,),
        config_revision=_DEFAULT_CONFIG_REVISION,
    )


def _route_summary(journey: Journey) -> str | None:
    """A short "line a → line b" summary of the transit legs."""
    lines = [leg.line for leg in _transit_legs(journey) if leg.line]
    if not lines:
        return None
    return " → ".join(lines)


def plan_transit(
    req: ArrivalRequirement,
    result: JourneyResult,
    profile: ProfileConfig,
    state: MissionState | None,
    previous: DeparturePlan | None,
    now: datetime,
    revision: int,
) -> DeparturePlan:
    """Build a public-transport departure plan for a mission (spec §6.1-§7).

    Filters the planner's journey candidates to those whose transfers all clear
    ``min_transfer``, whose first boarding is still reachable from ``now`` and
    whose final arrival meets the deadline; selects the latest safe home
    departure (fewer transfers wins near-ties); sticks to the previously chosen
    journey unless a candidate improves the home departure by more than
    :data:`STICKY_IMPROVEMENT_MINUTES`; and, when nothing arrives on time, falls
    back to the least-late journey with ``cannot_arrive_on_time``.
    """
    if result.status == "error":
        return _no_journey_plan(req, state, revision, REASON_NO_JOURNEY)
    if result.status == "empty" or not result.journeys:
        return _no_journey_plan(req, state, revision, REASON_NO_ON_TIME_JOURNEY)

    candidates = [
        candidate
        for journey in result.journeys
        if (candidate := _evaluate_journey(journey, req, profile, now)) is not None
    ]
    if not candidates:
        return _no_journey_plan(req, state, revision, REASON_NO_ON_TIME_JOURNEY)

    reasons: list[str] = []
    selected = _select_candidate(candidates)

    if selected is None:
        # No feasible journey: offer the least-late option (§6.2).
        best = _best_late(candidates)
        if best is None:
            return _no_journey_plan(req, state, revision, REASON_NO_ON_TIME_JOURNEY)
        return _build_transit_plan(
            req=req,
            candidate=best,
            profile=profile,
            state=state,
            result=result,
            now=now,
            revision=revision,
            feasible=False,
            last_on_time_alternative_leave=None,
            extra_reasons=[REASON_NO_ON_TIME_JOURNEY],
        )

    # Stickiness: keep the previously chosen journey unless another feasible
    # candidate lets us leave home more than STICKY_IMPROVEMENT_MINUTES later,
    # to avoid jumpy advice (§7).
    if previous is not None and previous.journey_id is not None:
        prev_match = next(
            (
                c
                for c in candidates
                if c.feasible and c.journey.journey_id == previous.journey_id
            ),
            None,
        )
        if prev_match is not None:
            improvement = (
                selected.latest_leave - prev_match.latest_leave
            ).total_seconds() / 60
            if improvement <= STICKY_IMPROVEMENT_MINUTES:
                selected = prev_match
                reasons.append(REASON_KEPT_PREVIOUS_JOURNEY)

    alternative = _latest_on_time_alternative(selected, candidates)
    if alternative is not None:
        reasons.append(REASON_LATE_ALTERNATIVE_AVAILABLE)

    return _build_transit_plan(
        req=req,
        candidate=selected,
        profile=profile,
        state=state,
        result=result,
        now=now,
        revision=revision,
        feasible=True,
        last_on_time_alternative_leave=alternative,
        extra_reasons=reasons,
    )


def _build_transit_plan(
    *,
    req: ArrivalRequirement,
    candidate: _CandidateJourney,
    profile: ProfileConfig,
    state: MissionState | None,
    result: JourneyResult,
    now: datetime,
    revision: int,
    feasible: bool,
    last_on_time_alternative_leave: datetime | None,
    extra_reasons: list[str],
) -> DeparturePlan:
    """Assemble a :class:`DeparturePlan` from a selected transit candidate."""
    journey = candidate.journey
    latest_leave = candidate.latest_leave
    recommended_leave = latest_leave - timedelta(minutes=profile.margins.departure)

    transit_legs = _transit_legs(journey)
    first_dep = _effective_departure(transit_legs[0])
    # ``first_dep`` is non-None here: _evaluate_journey rejected journeys without
    # it. The in-vehicle + waiting time from first boarding to final arrival.
    assert first_dep is not None
    travel_minutes = int(
        round((candidate.predicted_arrival - first_dep).total_seconds() / 60)
    )
    access_walk = _access_walk_minutes(journey)

    breakdown = MarginBreakdown(
        travel=max(travel_minutes, 0),
        access_walk=access_walk,
        boarding=profile.margins.boarding,
        departure=profile.margins.departure,
        arrival=profile.margins.arrival,
        extra_after=0,
    )

    has_realtime = _journey_has_realtime(journey)
    quality: Quality = "realtime" if has_realtime else "scheduled"
    reasons = list(extra_reasons)
    if has_realtime:
        reasons.append(REASON_REALTIME)

    status = _derive_status(now, recommended_leave, latest_leave, state)
    if not feasible and status not in ("departed", "skipped"):
        status = "cannot_arrive_on_time"

    return DeparturePlan(
        mission_id=req.mission_id,
        plan_id=req.mission_id,
        revision=revision,
        requirement=req,
        mode="public_transport",
        recommended_leave=recommended_leave,
        latest_leave=latest_leave,
        last_on_time_alternative_leave=last_on_time_alternative_leave,
        predicted_arrival=candidate.predicted_arrival,
        journey_id=journey.journey_id,
        route_summary=_route_summary(journey),
        quality=quality,
        feasible=feasible,
        status=status,
        breakdown=breakdown,
        reason_codes=tuple(reasons),
        config_revision=_DEFAULT_CONFIG_REVISION,
    )
