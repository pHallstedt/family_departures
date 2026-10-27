"""Tests for the frozen domain models and their validation."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, time

import pytest
from custom_components.family_departures.models import (
    ArrivalRequirement,
    CalendarLink,
    CalendarSource,
    DayOverride,
    DeparturePlan,
    DurationResult,
    EventTags,
    HouseholdConfig,
    JourneyResult,
    Leg,
    LinkRule,
    Location,
    LunchMenu,
    MarginBreakdown,
    Margins,
    MissionState,
    NotificationIntent,
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
        default_location_id="school",
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


# --- New pure models (step 1) ---------------------------------------------


def test_event_tags_defaults_empty() -> None:
    tags = EventTags()
    assert tags.location is None
    assert tags.transport is None
    assert tags.attendees == ()


def test_schedule_event_additive_fields_default() -> None:
    event = ScheduleEvent(uid="u", summary="s", start=T0, end=T1, source_id="src")
    assert event.location is None
    assert event.tags == EventTags()
    assert event.occurrence_key is None


def test_schedule_event_trims_and_caps_location() -> None:
    event = ScheduleEvent(
        uid="u",
        summary="s",
        start=T0,
        end=T1,
        source_id="src",
        location="  " + "x" * 100 + "  ",
    )
    assert event.location is not None
    assert len(event.location) == 80


def test_schedule_event_blank_location_becomes_none() -> None:
    event = ScheduleEvent(
        uid="u", summary="s", start=T0, end=T1, source_id="src", location="   "
    )
    assert event.location is None


def test_location_rejects_negative_static_minutes() -> None:
    with pytest.raises(ValueError, match="static_minutes"):
        Location(id="loc", name="Pool", lat=59.0, lon=18.0, static_minutes=-1)


def test_location_defaults() -> None:
    loc = Location(id="loc", name="Pool", lat=59.0, lon=18.0)
    assert loc.aliases == ()
    assert loc.static_minutes is None


def test_calendar_source_rejects_bad_type() -> None:
    with pytest.raises(ValueError, match="source_type"):
        CalendarSource(id="c", name="Schema", source_type="webcal")  # type: ignore[arg-type]


def test_calendar_source_defaults_entity_none() -> None:
    cal = CalendarSource(id="c", name="Schema", source_type="ics")
    assert cal.calendar_entity_id is None


def test_link_rule_rejects_bad_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        LinkRule(match="Simning", mode="plane")  # type: ignore[arg-type]


def test_calendar_link_rejects_bad_role() -> None:
    with pytest.raises(ValueError, match="role"):
        CalendarLink(link_id="l1", calendar_id="c1", role="primary")  # type: ignore[arg-type]


def test_calendar_link_rejects_bad_attendee_mode() -> None:
    with pytest.raises(ValueError, match="attendee_mode"):
        CalendarLink(
            link_id="l1",
            calendar_id="c1",
            role="extra",
            attendee_mode="only_me",  # type: ignore[arg-type]
        )


def test_calendar_link_rejects_bad_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        CalendarLink(link_id="l1", calendar_id="c1", role="extra", mode="boat")  # type: ignore[arg-type]


def test_calendar_link_rejects_negative_arrival_margin() -> None:
    with pytest.raises(ValueError, match="arrival_margin"):
        CalendarLink(link_id="l1", calendar_id="c1", role="extra", arrival_margin=-5)


def test_calendar_link_rejects_negative_static_minutes() -> None:
    with pytest.raises(ValueError, match="static_minutes"):
        CalendarLink(link_id="l1", calendar_id="c1", role="extra", static_minutes=-1)


def test_calendar_link_defaults() -> None:
    link = CalendarLink(link_id="l1", calendar_id="c1", role="main")
    assert link.label == ""
    assert link.source_filter == SourceFilter()
    assert link.attendee_mode == "all"
    assert link.location_id is None
    assert link.mode is None
    assert link.rules == ()
    assert link.match_event_location is False
    assert link.show_summary is False


def test_calendar_link_slot_for_is_colon_and_pipe_free() -> None:
    link = CalendarLink(link_id="football", calendar_id="c1", role="extra")
    slot = link.slot_for("abc123def4")
    assert slot == "x-football-abc123def4"
    assert ":" not in slot
    assert "|" not in slot


def test_lunch_menu_rejects_bad_status() -> None:
    with pytest.raises(ValueError, match="status"):
        LunchMenu(
            local_date=date(2026, 10, 1),
            status="pending",  # type: ignore[arg-type]
            dishes=(),
            fetched_at=T0,
        )


def test_lunch_menu_rejects_naive_fetched_at() -> None:
    with pytest.raises(ValueError, match="fetched_at"):
        LunchMenu(
            local_date=date(2026, 10, 1),
            status="ok",
            dishes=("Pannkakor",),
            fetched_at=datetime(2026, 10, 1),
        )


def test_household_config_holds_registries() -> None:
    loc = Location(id="school", name="Skola", lat=59.0, lon=18.0)
    cal = CalendarSource(id="c1", name="Schema", source_type="ics")
    household = HouseholdConfig(
        home=(59.3, 18.0),
        locations={"school": loc},
        calendars={"c1": cal},
        profiles={"kid_a": _profile()},
    )
    assert household.home == (59.3, 18.0)
    assert household.locations["school"] is loc
    assert household.calendars["c1"] is cal
    assert household.profiles["kid_a"].id == "kid_a"


def test_arrival_requirement_additive_fields_default() -> None:
    req = _requirement()
    assert req.slot == "morning"
    assert req.link_id is None
    assert req.mode is None
    assert req.label is None
    assert req.destination_name is None
    assert req.warnings == ()


def test_arrival_requirement_rejects_bad_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        ArrivalRequirement(
            mission_id="m",
            person_id="kid_a",
            local_date=date(2026, 10, 1),
            event_id="e1",
            event_start=T1,
            arrival_deadline=T0,
            destination_id="school",
            source="ics",
            mode="submarine",  # type: ignore[arg-type]
        )


def test_notification_intent_additive_fields_default() -> None:
    intent = NotificationIntent(
        person_id="kid_a",
        mission_id="m",
        plan_id="p",
        revision=1,
        notification_id="n",
        kind="morning",
        severity="info",
        title="t",
        message="msg",
        recommended_leave_time=None,
        latest_leave_time=None,
        quality="scheduled",
        reason_codes=(),
        packing_items=(),
        channels=(),
        tag="tag",
        action_nonce="nonce",
    )
    assert intent.lunch_menu is None
    assert intent.slot == "morning"


def test_calendar_link_slot_for_sanitises_hostile_keys() -> None:
    link = CalendarLink(link_id="club", calendar_id="c1", role="extra")
    slot = link.slot_for("k:1|2")
    assert slot == "x-club-k-1-2"
    assert ":" not in slot
    assert "|" not in slot


@pytest.mark.parametrize("bad", ["", "a:b", "a|b", "a b", "å"])
def test_calendar_link_rejects_ids_that_break_mission_id_parsing(bad: str) -> None:
    with pytest.raises(ValueError, match="link_id"):
        CalendarLink(link_id=bad, calendar_id="c1", role="extra")
