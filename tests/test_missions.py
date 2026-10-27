"""Scenario table for multi-mission selection (plan §11 item 5, scenarios a-l)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.family_departures.missions import (
    REASON_EXTRA_SOURCE_ERROR,
    REASON_UNKNOWN_LINK_LOCATION,
    REASON_UNKNOWN_LOCATION_TAG,
    REASON_UNKNOWN_TRANSPORT_TAG,
    WARNING_HOME_GAP_TOO_SHORT,
    WARNING_OVERLAPS_OTHER,
    DayMissions,
    annotate_conflicts,
    resolve_destination,
    select_missions,
)
from custom_components.family_departures.models import (
    CalendarLink,
    DayOverride,
    DeparturePlan,
    EventTags,
    LinkRule,
    Location,
    Margins,
    MissionState,
    ProfileConfig,
    ScheduleEvent,
    ScheduleResult,
)
from custom_components.family_departures.schedule import (
    REASON_EMPTY_SCHEDULE,
    REASON_FIRST_EVENT,
    REASON_LOCKED_AFTER_DEPARTURE,
    REASON_OVERRIDE_OFF,
    select_requirement,
)

TZ = ZoneInfo("Europe/Stockholm")
THURSDAY = date(2026, 10, 1)
SATURDAY = date(2026, 10, 3)
NOW = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)
NO_TAGS = EventTags()

SCHOOL = CalendarLink(link_id="school", calendar_id="cal_school", role="main")
FOOTBALL = CalendarLink(
    link_id="football", calendar_id="cal_club", role="extra", label="Fotboll"
)

LOCATIONS = {
    "school": Location("school", "Skolan", 59.0, 18.0),
    "pitch": Location("pitch", "Fotbollsplanen", 59.1, 18.1, aliases=("Plan 3",)),
    "pool": Location("pool", "Badhuset", 59.2, 18.2, aliases=("Simhall", "Simhallen")),
}


def _local(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ).astimezone(UTC)


def _event(
    d: date,
    hh: int,
    mm: int,
    *,
    summary: str = "Aktivitet",
    uid: str,
    minutes: int = 60,
    key: str | None = None,
    location: str | None = None,
    tags: EventTags = NO_TAGS,
) -> ScheduleEvent:
    start = _local(d, hh, mm)
    return ScheduleEvent(
        uid=uid,
        summary=summary,
        start=start,
        end=start + timedelta(minutes=minutes),
        source_id="cal",
        location=location,
        tags=tags,
        occurrence_key=key,
    )


def _result(*events: ScheduleEvent, status: str = "ok") -> ScheduleResult:
    resolved = status if status != "ok" or events else "empty"
    return ScheduleResult(
        status=resolved,  # type: ignore[arg-type]
        events=tuple(events),
        fetched_at=NOW,
    )


def _profile(*links: CalendarLink) -> ProfileConfig:
    return ProfileConfig(
        id="kid_b",
        name="Kid B",
        default_location_id="school",
        default_mode="public_transport",
        static_minutes=None,
        static_label=None,
        weather_adjust=False,
        car_fallback_minutes=None,
        margins=Margins(
            arrival=5, departure=0, boarding=0, parking_and_walk=0, min_transfer=5
        ),
        weekday_mask=frozenset({0, 1, 2, 3, 4}),
        packing_rules=(),
        person_entity_id=None,
        notifications_enabled=True,
        change_threshold_minutes=5,
        quiet_start=time(21, 0),
        quiet_end=time(6, 0),
        scripts={},
        calendar_links=links,
    )


def _select(
    profile: ProfileConfig,
    link_days: dict[str, ScheduleResult],
    *,
    d: date = THURSDAY,
    override: DayOverride | None = None,
    holiday: bool = False,
    previous: tuple[ScheduleEvent, ...] | None = None,
    states: dict[str, MissionState] | None = None,
) -> DayMissions:
    return select_missions(
        profile,
        d,
        link_days,
        override,
        holiday,
        previous,
        states or {},
        LOCATIONS,
        NOW,
    )


def _slots(day: DayMissions) -> list[str]:
    return [r.slot for r in day.requirements]


# --- (a) school only: identical to the single-calendar behaviour -------------


def test_a_school_only_is_identical_to_select_requirement() -> None:
    profile = _profile(SCHOOL)
    lesson = _event(THURSDAY, 8, 20, summary="Lektion", uid="a")
    day = _select(profile, {"school": _result(lesson)})

    expected = select_requirement(
        profile, THURSDAY, _result(lesson), None, False, None, None, NOW, SCHOOL
    )
    assert day.day_status == "has_event"
    assert len(day.requirements) == 1
    assert day.requirements[0] == expected.requirement
    assert day.requirements[0].slot == "morning"
    assert day.missions[0].outcome.reason_codes == (REASON_FIRST_EVENT,)


# --- (b) school + two football events -> three missions, origin home ---------


def test_b_school_plus_two_extra_events_gives_three_missions() -> None:
    profile = _profile(SCHOOL, FOOTBALL)
    day = _select(
        profile,
        {
            "school": _result(_event(THURSDAY, 8, 20, uid="a", minutes=460)),
            "football": _result(
                _event(THURSDAY, 19, 30, uid="f2", key="k2"),
                _event(THURSDAY, 17, 30, uid="f1", key="k1"),
            ),
        },
    )
    assert day.day_status == "has_event"
    assert _slots(day) == ["morning", "x-football-k1", "x-football-k2"]
    first, second = day.requirements[1], day.requirements[2]
    assert first.mission_id == "kid_b:2026-10-01:x-football-k1"
    assert first.arrival_deadline == _local(THURSDAY, 17, 25)
    assert second.arrival_deadline == _local(THURSDAY, 19, 25)
    assert first.label == "Fotboll"
    assert first.link_id == "football"


def test_extra_link_arrival_margin_and_show_summary() -> None:
    link = CalendarLink(
        link_id="club",
        calendar_id="c",
        role="extra",
        arrival_margin=20,
        show_summary=True,
    )
    day = _select(
        _profile(link),
        {
            "club": _result(
                _event(THURSDAY, 18, 0, summary="Träning [plats: x]", uid="u", key="k")
            )
        },
    )
    req = day.requirements[0]
    assert req.arrival_deadline == _local(THURSDAY, 17, 40)
    assert req.label == "Träning"


# --- (c) Saturday with extra events only -------------------------------------


def test_c_saturday_extra_only_has_event_and_main_is_silent() -> None:
    profile = _profile(SCHOOL, FOOTBALL)
    day = _select(
        profile,
        {
            "school": _result(),
            "football": _result(_event(SATURDAY, 10, 0, uid="m", key="k")),
        },
        d=SATURDAY,
    )
    assert day.day_status == "has_event"
    assert _slots(day) == ["x-football-k"]
    main = day.missions[0].outcome
    assert main.day_status == "no_activity"
    assert main.requirement is None


def test_extra_event_on_holiday_is_still_a_mission() -> None:
    profile = _profile(SCHOOL, FOOTBALL)
    day = _select(
        profile,
        {
            "school": _result(),
            "football": _result(_event(THURSDAY, 17, 30, uid="m", key="k")),
        },
        holiday=True,
    )
    assert day.day_status == "has_event"
    assert day.missions[0].outcome.day_status == "day_off"
    assert _slots(day) == ["x-football-k"]


def test_empty_extra_calendar_is_silent_without_main() -> None:
    day = _select(_profile(FOOTBALL), {"football": _result()})
    assert day.day_status == "no_activity"
    assert day.missions == ()
    assert REASON_EMPTY_SCHEDULE not in day.reason_codes


# --- (d) back-to-back: home_gap_too_short ------------------------------------


def _plan(day: DayMissions, index: int, leave: datetime | None) -> DeparturePlan:
    req = day.requirements[index]
    return DeparturePlan(
        mission_id=req.mission_id,
        plan_id="p",
        revision=1,
        requirement=req,
        mode="public_transport",
        recommended_leave=leave,
        latest_leave=leave,
        last_on_time_alternative_leave=None,
        predicted_arrival=None,
        journey_id=None,
        route_summary=None,
        quality="scheduled",
        feasible=True,
        status="scheduled",
        breakdown=None,
        reason_codes=("planned",),
        config_revision=1,
    )


def test_d_home_gap_too_short_when_leave_precedes_last_lesson_end() -> None:
    profile = _profile(SCHOOL, FOOTBALL)
    lesson = _event(THURSDAY, 16, 10, uid="last", minutes=60)  # ends 17:10
    practice = _event(THURSDAY, 17, 30, uid="f1", key="k1")
    day = _select(profile, {"school": _result(lesson), "football": _result(practice)})
    plan = _plan(day, 1, _local(THURSDAY, 16, 55))

    [annotated] = annotate_conflicts(
        [plan], {plan.mission_id: practice}, [lesson, practice]
    )
    assert annotated.requirement.warnings == (WARNING_HOME_GAP_TOO_SHORT,)
    assert WARNING_HOME_GAP_TOO_SHORT in annotated.reason_codes
    assert annotated.recommended_leave == plan.recommended_leave


def test_d_no_flag_when_leave_is_after_previous_end() -> None:
    lesson = _event(THURSDAY, 8, 20, uid="last", minutes=60)
    practice = _event(THURSDAY, 17, 30, uid="f1", key="k1")
    day = _select(
        _profile(SCHOOL, FOOTBALL),
        {"school": _result(lesson), "football": _result(practice)},
    )
    plan = _plan(day, 1, _local(THURSDAY, 16, 55))
    [annotated] = annotate_conflicts(
        [plan], {plan.mission_id: practice}, [lesson, practice]
    )
    assert annotated == plan


def test_school_lessons_never_flag_each_other() -> None:
    first = _event(THURSDAY, 8, 20, uid="a")
    second = _event(THURSDAY, 8, 30, uid="b")  # overlaps the first
    day = _select(_profile(SCHOOL), {"school": _result(first, second)})
    plan = _plan(day, 0, _local(THURSDAY, 8, 0))
    [annotated] = annotate_conflicts([plan], {plan.mission_id: first}, [first, second])
    assert annotated == plan


# --- (e) simultaneous extra events -------------------------------------------


def test_e_two_simultaneous_extra_events_flag_each_other() -> None:
    profile = _profile(FOOTBALL)
    a = _event(THURSDAY, 17, 30, uid="a", key="ka")
    b = _event(THURSDAY, 17, 30, uid="b", key="kb")
    day = _select(profile, {"football": _result(a, b)})
    plans = [_plan(day, 0, _local(THURSDAY, 17, 0)), _plan(day, 1, None)]
    events = {plans[0].mission_id: a, plans[1].mission_id: b}

    result = annotate_conflicts(plans, events, [a, b])
    assert [p.requirement.warnings for p in result] == [
        (WARNING_OVERLAPS_OTHER,),
        (WARNING_OVERLAPS_OTHER,),
    ]


def test_e_identical_uids_on_ha_style_events_still_flag_each_other() -> None:
    a = _event(THURSDAY, 17, 30, uid="same", key="k", summary="Fotboll")
    b = _event(THURSDAY, 17, 30, uid="same", key="k-2", summary="Fotboll")
    day = _select(_profile(FOOTBALL), {"football": _result(a, b)})
    plans = [_plan(day, 0, None), _plan(day, 1, None)]
    events = {plans[0].mission_id: a, plans[1].mission_id: b}
    result = annotate_conflicts(plans, events, [a, b])
    assert all(p.requirement.warnings == (WARNING_OVERLAPS_OTHER,) for p in result)


def test_e_partial_overlap_is_flagged_but_back_to_back_is_not() -> None:
    a = _event(THURSDAY, 17, 0, uid="a", key="ka", minutes=60)
    b = _event(THURSDAY, 17, 30, uid="b", key="kb", minutes=60)
    c = _event(THURSDAY, 18, 30, uid="c", key="kc", minutes=60)  # starts as b ends
    day = _select(_profile(FOOTBALL), {"football": _result(a, b, c)})
    plans = [_plan(day, i, None) for i in range(3)]
    events = {p.mission_id: e for p, e in zip(plans, (a, b, c), strict=True)}
    result = annotate_conflicts(plans, events, [a, b, c])
    assert [p.requirement.warnings for p in result] == [
        (WARNING_OVERLAPS_OTHER,),
        (WARNING_OVERLAPS_OTHER,),
        (),
    ]


# --- (f) extra source error ---------------------------------------------------


def test_f_extra_error_leaves_main_unaffected() -> None:
    profile = _profile(SCHOOL, FOOTBALL)
    lesson = _event(THURSDAY, 8, 20, uid="a")
    day = _select(
        profile,
        {"school": _result(lesson), "football": _result(status="error")},
    )
    assert day.day_status == "has_event"
    assert _slots(day) == ["morning"]
    assert REASON_EXTRA_SOURCE_ERROR in day.reason_codes


def test_f_missing_extra_result_counts_as_error() -> None:
    day = _select(_profile(SCHOOL, FOOTBALL), {"school": _result()}, d=SATURDAY)
    assert REASON_EXTRA_SOURCE_ERROR in day.reason_codes
    assert day.day_status == "no_activity"


# --- (g) main source error ----------------------------------------------------


def test_g_main_error_still_plans_extras_and_keeps_the_error() -> None:
    profile = _profile(SCHOOL, FOOTBALL)
    day = _select(
        profile,
        {
            "school": _result(status="error"),
            "football": _result(_event(THURSDAY, 17, 30, uid="f", key="k")),
        },
    )
    assert day.day_status == "source_error"
    assert _slots(day) == ["x-football-k"]
    assert day.missions[0].outcome.day_status == "source_error"


# --- (h) attendance -----------------------------------------------------------


def test_h_off_and_sick_close_every_mission() -> None:
    profile = _profile(SCHOOL, FOOTBALL)
    link_days = {
        "school": _result(_event(THURSDAY, 8, 20, uid="a")),
        "football": _result(_event(THURSDAY, 17, 30, uid="f", key="k")),
    }
    for attendance in ("off", "sick"):
        day = _select(
            profile,
            link_days,
            override=DayOverride("kid_b", THURSDAY, attendance=attendance),  # type: ignore[arg-type]
        )
        assert day.day_status == "day_off"
        assert day.requirements == ()
        assert REASON_OVERRIDE_OFF in day.reason_codes


def test_h_off_without_main_link_closes_extras() -> None:
    day = _select(
        _profile(FOOTBALL),
        {"football": _result(_event(THURSDAY, 17, 30, uid="f", key="k"))},
        override=DayOverride("kid_b", THURSDAY, attendance="sick"),
    )
    assert day.day_status == "day_off"
    assert day.requirements == ()


def test_h_remote_closes_main_only() -> None:
    profile = _profile(SCHOOL, FOOTBALL)
    day = _select(
        profile,
        {
            "school": _result(_event(THURSDAY, 8, 20, uid="a")),
            "football": _result(_event(THURSDAY, 17, 30, uid="f", key="k")),
        },
        override=DayOverride("kid_b", THURSDAY, attendance="remote"),
    )
    assert _slots(day) == ["x-football-k"]
    assert day.day_status == "has_event"
    assert day.missions[0].outcome.day_status == "day_off"


def test_h_remote_without_extras_is_day_off() -> None:
    day = _select(
        _profile(SCHOOL),
        {"school": _result(_event(THURSDAY, 8, 20, uid="a"))},
        override=DayOverride("kid_b", THURSDAY, attendance="remote"),
    )
    assert day.day_status == "day_off"


# --- (i) precedence -----------------------------------------------------------


def _event_with(
    *,
    tags: EventTags = NO_TAGS,
    location: str | None = None,
    summary: str = "Träning",
) -> ScheduleEvent:
    return _event(
        THURSDAY, 18, 0, uid="u", key="k", summary=summary, tags=tags, location=location
    )


def _extra(**kwargs: object) -> CalendarLink:
    return CalendarLink(link_id="club", calendar_id="c", role="extra", **kwargs)  # type: ignore[arg-type]


def _requirement(link: CalendarLink, event: ScheduleEvent, **kwargs: object):  # type: ignore[no-untyped-def]
    day = _select(
        _profile(link),
        {"club": _result(event)},
        override=kwargs.get("override"),  # type: ignore[arg-type]
    )
    return day.requirements[0], day


def test_i_destination_precedence_chain() -> None:
    profile = _profile(SCHOOL)
    link = _extra(
        location_id="pool",
        match_event_location=True,
        rules=(LinkRule("träning", location_id="pitch"),),
    )
    tagged = _event_with(tags=EventTags(location="Badhuset"), location="Plan 3")
    with_rule = _event_with(location="Simhall")
    only_location = _event_with(summary="Annat", location="Simhallen, Plan 3")
    plain = _event_with(summary="Annat")

    assert resolve_destination(profile, link, tagged, LOCATIONS) == ("pool", ())
    assert resolve_destination(profile, link, with_rule, LOCATIONS) == ("pitch", ())
    # Longest alias contained in LOCATION wins ("Simhallen" over "Plan 3"/"Simhall").
    assert resolve_destination(profile, link, only_location, LOCATIONS) == ("pool", ())
    assert resolve_destination(profile, link, plain, LOCATIONS) == ("pool", ())
    bare = _extra()
    assert resolve_destination(profile, bare, plain, LOCATIONS) == ("school", ())


def test_i_tag_matches_id_name_and_alias_casefolded() -> None:
    profile = _profile(SCHOOL)
    for value in ("PITCH", "fotbollsplanen", "plan 3"):
        event = _event_with(tags=EventTags(location=value))
        assert resolve_destination(profile, _extra(), event, LOCATIONS)[0] == "pitch"


def test_i_location_field_ignored_without_opt_in() -> None:
    event = _event_with(location="Simhallen")
    assert resolve_destination(_profile(SCHOOL), _extra(), event, LOCATIONS) == (
        "school",
        (),
    )


def test_i_mode_precedence_chain() -> None:
    link = _extra(mode="car", rules=(LinkRule("träning", mode="static"),))
    override = DayOverride("kid_b", THURSDAY, mode="public_transport")

    req, _ = _requirement(link, _event_with(tags=EventTags(transport="cykel")))
    assert req.mode == "static"  # tag (cykel -> static)

    req, _ = _requirement(link, _event_with(summary="Träning"))
    assert req.mode == "static"  # rule

    req, _ = _requirement(link, _event_with(summary="Annat"))
    assert req.mode == "car"  # link default

    req, _ = _requirement(_extra(), _event_with(summary="Annat"))
    assert req.mode is None  # nothing overrides: profile default applies

    req, _ = _requirement(
        link, _event_with(tags=EventTags(transport="bil")), override=override
    )
    assert req.mode == "public_transport"  # day override beats the tag


# --- (j) unknown tags fall back with a reason --------------------------------


def test_j_unknown_location_tag_falls_back_with_reason() -> None:
    link = _extra(location_id="pool")
    req, day = _requirement(link, _event_with(tags=EventTags(location="Mars")))
    assert req.destination_id == "pool"
    assert REASON_UNKNOWN_LOCATION_TAG in day.missions[0].outcome.reason_codes


def test_j_unknown_transport_tag_falls_back_with_reason() -> None:
    link = _extra(mode="car")
    req, day = _requirement(link, _event_with(tags=EventTags(transport="jetpack")))
    assert req.mode == "car"
    assert REASON_UNKNOWN_TRANSPORT_TAG in day.missions[0].outcome.reason_codes


def test_j_deleted_link_or_rule_location_falls_back_to_profile_default() -> None:
    link = _extra(location_id="gone")
    req, day = _requirement(link, _event_with(summary="Annat"))
    assert req.destination_id == "school"
    assert REASON_UNKNOWN_LINK_LOCATION in day.missions[0].outcome.reason_codes

    rule_link = _extra(rules=(LinkRule("träning", location_id="gone"),))
    req, day = _requirement(rule_link, _event_with())
    assert req.destination_id == "school"
    assert REASON_UNKNOWN_LINK_LOCATION in day.missions[0].outcome.reason_codes


def test_main_link_destination_follows_tags() -> None:
    profile = _profile(SCHOOL)
    lesson = _event(
        THURSDAY, 8, 20, uid="a", tags=EventTags(location="pool", transport="bil")
    )
    day = _select(profile, {"school": _result(lesson)})
    req = day.requirements[0]
    assert (req.destination_id, req.mode, req.slot) == ("pool", "car", "morning")


# --- (k) locked main mission is unaffected by an extra change ----------------


def _departed(mission_id: str) -> MissionState:
    return MissionState(
        mission_id=mission_id,
        status="departed",
        departed_at=NOW,
        reopened=False,
        notified={},
        first_published_leave=None,
        action_nonce="n",
    )


def test_k_locked_main_is_unaffected_by_an_extra_event_change() -> None:
    profile = _profile(SCHOOL, FOOTBALL)
    original = _event(THURSDAY, 8, 20, uid="a")
    states = {"kid_b:2026-10-01:morning": _departed("kid_b:2026-10-01:morning")}

    before = _select(
        profile,
        {
            "school": _result(original),
            "football": _result(_event(THURSDAY, 17, 30, uid="f", key="k")),
        },
        states=states,
    )
    after = _select(
        profile,
        {
            "school": _result(original),
            "football": _result(_event(THURSDAY, 19, 0, uid="f", key="k")),
        },
        states=states,
    )
    assert before.requirements[0] == after.requirements[0]
    assert REASON_LOCKED_AFTER_DEPARTURE in after.missions[0].outcome.reason_codes
    # The extra mission changed its time but kept its id (and so its ledger).
    assert before.requirements[1].mission_id == after.requirements[1].mission_id
    assert after.requirements[1].event_start == _local(THURSDAY, 19, 0)


# --- (l) stable ids -----------------------------------------------------------


def test_l_moved_event_keeps_id_and_removed_event_disappears() -> None:
    profile = _profile(FOOTBALL)
    early = _event(THURSDAY, 17, 30, uid="f", key="stable")
    later = _event(THURSDAY, 19, 0, uid="f", key="stable")

    first = _select(profile, {"football": _result(early)})
    moved = _select(profile, {"football": _result(later)})
    gone = _select(profile, {"football": _result()})

    assert first.requirements[0].mission_id == moved.requirements[0].mission_id
    assert moved.requirements[0].event_start == _local(THURSDAY, 19, 0)
    assert gone.requirements == ()


def test_missing_occurrence_key_falls_back_to_local_start() -> None:
    day = _select(
        _profile(FOOTBALL),
        {"football": _result(_event(THURSDAY, 17, 30, uid="f"))},
    )
    assert day.requirements[0].slot == "x-football-1730"


def test_duplicate_keys_get_a_suffix_and_slots_are_separator_free() -> None:
    link = CalendarLink(link_id="a-b", calendar_id="c", role="extra")
    day = _select(
        _profile(link),
        {
            "a-b": _result(
                _event(THURSDAY, 17, 0, uid="u1", key="same|key"),
                _event(THURSDAY, 18, 0, uid="u2", key="same|key"),
            )
        },
    )
    slots = _slots(day)
    assert slots == ["x-a-b-same-key", "x-a-b-same-key-2"]
    assert all(":" not in s and "|" not in s for s in slots)
    # The id still splits into person:date:slot.
    assert day.requirements[0].mission_id.split(":") == [
        "kid_b",
        "2026-10-01",
        "x-a-b-same-key",
    ]
