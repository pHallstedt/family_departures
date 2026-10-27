"""Tests for the notification policy (spec §12.1, §5.5, §11.3).

These assert product behaviour, not implementation detail: the right kind of
notification fires at the right clock time, change alerts compare against the
last notified leave time (so small drifts accumulate), improvements need two
stable revisions, quiet hours shift or skip the morning notice, packing items
ride the evening/morning notices and disappear once acknowledged, dedup is
keyed on ``(mission_id, kind)`` and a departed mission produces only cleanup.

All times are built in ``Europe/Stockholm`` and converted to UTC, and ``now``
is injected, so no wall clock is read.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from custom_components.family_departures.models import (
    ArrivalRequirement,
    DeparturePlan,
    LunchMenu,
    Margins,
    MissionState,
    NotifiedRecord,
    PackingList,
    ProfileConfig,
    Quality,
)
from custom_components.family_departures.notification_policy import (
    KIND_CHANGE,
    KIND_CLEANUP,
    KIND_CRITICAL,
    KIND_EVENING,
    KIND_LEAVE_NOW,
    KIND_MORNING,
    KIND_PACKING,
    KIND_REMINDER,
    KIND_REMOVED,
    REASON_IMPROVEMENT,
    REASON_WORSENING,
    evaluate,
    evaluate_day_digest,
    evaluate_removed,
)

TZ = ZoneInfo("Europe/Stockholm")
DAY = date(2026, 10, 20)  # a Tuesday
MISSION = "kid_b:2026-10-20:morning"


def _utc(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ).astimezone(UTC)


def _profile(
    *,
    notifications_enabled: bool = True,
    evening_notice_enabled: bool = True,
    lunch_in_digest: bool = True,
    change_threshold: int = 3,
    quiet_start: time = time(21, 0),
    quiet_end: time = time(6, 0),
    scripts: dict[str, str] | None = None,
) -> ProfileConfig:
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
            arrival=5, departure=5, boarding=2, parking_and_walk=0, min_transfer=5
        ),
        weekday_mask=frozenset({0, 1, 2, 3, 4}),
        packing_rules=(),
        person_entity_id=None,
        notifications_enabled=notifications_enabled,
        change_threshold_minutes=change_threshold,
        quiet_start=quiet_start,
        quiet_end=quiet_end,
        scripts=scripts if scripts is not None else {"push": "script.kid_b_push"},
        evening_notice_enabled=evening_notice_enabled,
        lunch_in_digest=lunch_in_digest,
    )


def _requirement(event_hh: int = 8, event_mm: int = 20) -> ArrivalRequirement:
    start = _utc(DAY, event_hh, event_mm)
    return ArrivalRequirement(
        mission_id=MISSION,
        person_id="kid_b",
        local_date=DAY,
        event_id="evt-1",
        event_start=start,
        arrival_deadline=start,
        destination_id="school",
        source="kid_b_ics",
    )


def _plan(
    *,
    recommended: datetime | None,
    latest: datetime | None = None,
    revision: int = 1,
    status: str = "scheduled",
    quality: Quality = "scheduled",
    feasible: bool = True,
    reason_codes: tuple[str, ...] = (),
    requirement: ArrivalRequirement | None = None,
) -> DeparturePlan:
    req = requirement if requirement is not None else _requirement()
    return DeparturePlan(
        mission_id=MISSION,
        plan_id=MISSION,
        revision=revision,
        requirement=req,
        mode="public_transport",
        recommended_leave=recommended,
        latest_leave=latest if latest is not None else recommended,
        last_on_time_alternative_leave=None,
        predicted_arrival=req.event_start,
        journey_id="j1",
        route_summary="buss 123",
        quality=quality,
        feasible=feasible,
        status=status,  # type: ignore[arg-type]
        breakdown=None,
        reason_codes=reason_codes,
        config_revision=0,
    )


def _state(
    *,
    status: str = "scheduled",
    departed_at: datetime | None = None,
    notified: dict[str, NotifiedRecord] | None = None,
    first_published_leave: datetime | None = None,
) -> MissionState:
    return MissionState(
        mission_id=MISSION,
        status=status,  # type: ignore[arg-type]
        departed_at=departed_at,
        reopened=False,
        notified=notified if notified is not None else {},
        first_published_leave=first_published_leave,
        action_nonce="nonce-1",
    )


def _packing(
    items: tuple[str, ...] = (), acknowledged: tuple[str, ...] = ()
) -> PackingList:
    return PackingList(
        person_id="kid_b",
        local_date=DAY,
        items=items,
        acknowledged=acknowledged,
    )


def _kinds(intents: list) -> set[str]:
    return {i.kind for i in intents}


# --- Disabled / closed missions -------------------------------------------


def test_notifications_disabled_yields_nothing() -> None:
    rec = _utc(DAY, 7, 32)
    intents = evaluate(
        None,
        _plan(recommended=rec),
        _state(),
        _packing(),
        _profile(notifications_enabled=False),
        now=rec,
    )
    assert intents == []


def test_no_intents_after_departed_except_cleanup() -> None:
    """A departed mission produces only a one-off cleanup (§12.1)."""
    rec = _utc(DAY, 7, 32)
    state = _state(status="departed", departed_at=_utc(DAY, 7, 30))
    intents = evaluate(
        None, _plan(recommended=rec), state, _packing(), _profile(), now=rec
    )
    assert _kinds(intents) == {KIND_CLEANUP}

    # Once cleanup is recorded, nothing is produced.
    state2 = _state(
        status="departed",
        departed_at=_utc(DAY, 7, 30),
        notified={
            KIND_CLEANUP: NotifiedRecord(
                kind=KIND_CLEANUP, sent_at=rec, leave_time=None, revision=1
            )
        },
    )
    assert (
        evaluate(None, _plan(recommended=rec), state2, _packing(), _profile(), now=rec)
        == []
    )


# --- Timed morning / reminder / leave-now ----------------------------------


def test_morning_fires_60_minutes_before() -> None:
    rec = _utc(DAY, 7, 32)
    morning_at = _utc(DAY, 6, 32)
    intents = evaluate(
        None, _plan(recommended=rec), _state(), _packing(), _profile(), now=morning_at
    )
    assert KIND_MORNING in _kinds(intents)


def test_reminder_fires_10_minutes_before() -> None:
    rec = _utc(DAY, 7, 32)
    reminder_at = _utc(DAY, 7, 22)
    # Morning already sent so only reminder is new.
    state = _state(
        notified={
            KIND_MORNING: NotifiedRecord(
                kind=KIND_MORNING, sent_at=_utc(DAY, 6, 32), leave_time=rec, revision=1
            )
        }
    )
    intents = evaluate(
        None, _plan(recommended=rec), state, _packing(), _profile(), now=reminder_at
    )
    assert KIND_REMINDER in _kinds(intents)
    assert KIND_MORNING not in _kinds(intents)


def test_leave_now_fires_at_recommended() -> None:
    rec = _utc(DAY, 7, 32)
    intents = evaluate(
        None,
        _plan(recommended=rec, status="leave_now"),
        _state(),
        _packing(),
        _profile(),
        now=rec,
    )
    assert KIND_LEAVE_NOW in _kinds(intents)


def test_timed_notices_skip_days_other_than_today() -> None:
    """Morning/reminder/leave-now are only for today's date (§5.3)."""
    rec = _utc(DAY, 7, 32)
    # now is the day before, at the same wall-clock minute.
    yesterday_now = _utc(date(2026, 10, 19), 7, 32)
    intents = evaluate(
        None,
        _plan(recommended=rec),
        _state(),
        _packing(),
        _profile(),
        now=yesterday_now,
    )
    assert KIND_MORNING not in _kinds(intents)
    assert KIND_LEAVE_NOW not in _kinds(intents)


# --- Quiet hours -----------------------------------------------------------


def test_morning_held_inside_quiet_hours() -> None:
    """A morning notice computed at 05:45 waits for quiet-end 06:00 (§12.1)."""
    rec = _utc(DAY, 6, 45)  # recommended 06:45, morning at 05:45
    at_0545 = _utc(DAY, 5, 45)
    intents = evaluate(
        None, _plan(recommended=rec), _state(), _packing(), _profile(), now=at_0545
    )
    assert KIND_MORNING not in _kinds(intents)

    # At 06:00 (quiet-end) it is released; departure is 45 min away (> 15).
    at_0600 = _utc(DAY, 6, 0)
    intents2 = evaluate(
        None, _plan(recommended=rec), _state(), _packing(), _profile(), now=at_0600
    )
    assert KIND_MORNING in _kinds(intents2)


def test_morning_skipped_when_departure_too_close_after_quiet() -> None:
    """If < 15 min remain at quiet-end the morning notice is skipped (§12.1)."""
    rec = _utc(DAY, 6, 10)  # recommended 06:10; quiet ends 06:00 → 10 min left
    at_0600 = _utc(DAY, 6, 0)
    intents = evaluate(
        None, _plan(recommended=rec), _state(), _packing(), _profile(), now=at_0600
    )
    assert KIND_MORNING not in _kinds(intents)


def test_reminder_passes_quiet_hours() -> None:
    """Reminder for today's active trip always passes quiet hours (§12.1)."""
    rec = _utc(DAY, 5, 55)  # inside quiet; reminder at 05:45
    at_0545 = _utc(DAY, 5, 45)
    intents = evaluate(
        None, _plan(recommended=rec), _state(), _packing(), _profile(), now=at_0545
    )
    assert KIND_REMINDER in _kinds(intents)


# --- Day-before digest -------------------------------------------------------

DIGEST_ID = "kid_b:2026-10-20:day"
EVENING = _utc(date(2026, 10, 19), 20, 0)


def _digest_state(notified: dict[str, NotifiedRecord] | None = None) -> MissionState:
    return replace(_state(notified=notified), mission_id=DIGEST_ID)


def _digest(
    plans: list[DeparturePlan],
    *,
    items: tuple[str, ...] = (),
    profile: ProfileConfig | None = None,
    state: MissionState | None = None,
    legacy: MissionState | None = None,
    lunch: LunchMenu | None = None,
    now: datetime = EVENING,
) -> list:
    return evaluate_day_digest(
        profile or _profile(),
        DAY,
        plans,
        _packing(items=items),
        lunch,
        state or _digest_state(),
        legacy,
        now,
    )


def _school_plan() -> DeparturePlan:
    return _plan(recommended=_utc(DAY, 7, 45))


def test_digest_lists_missions_in_start_order_with_packing_and_warning() -> None:
    football = replace(
        _extra_plan(_utc(DAY, 16, 55)),
        requirement=replace(
            _extra_plan(_utc(DAY, 16, 55)).requirement,
            warnings=("home_gap_too_short",),
        ),
    )
    [digest] = _digest([football, _school_plan()], items=("Gympakläder",))

    assert digest.kind == KIND_EVENING
    assert digest.mission_id == DIGEST_ID
    assert digest.slot == "day"
    assert digest.tag == f"departure_{DIGEST_ID}"
    assert digest.action_nonce == "nonce-1"
    assert digest.message.split("\n") == [
        "08:20 Skola – gå 07:45",
        "17:30 Fotboll → Planen – gå 16:55 ⚠ kort tid hemma",
        "Packa: Gympakläder",
    ]
    assert digest.packing_items == ("Gympakläder",)
    # The headline departure is the earliest one.
    assert digest.recommended_leave_time == _utc(DAY, 7, 45)


def test_digest_includes_lunch_when_known() -> None:
    lunch = LunchMenu(
        local_date=DAY,
        status="ok",
        dishes=("Pasta", "Sallad"),
        fetched_at=_utc(DAY, 6, 0),
    )
    [digest] = _digest([_school_plan()], lunch=lunch)
    assert digest.message.endswith("Lunch imorgon: Pasta, Sallad")
    assert digest.lunch_menu == "Lunch imorgon: Pasta, Sallad"

    empty = replace(lunch, status="empty", dishes=())
    [no_lunch] = _digest([_school_plan()], lunch=empty)
    assert "Lunch" not in no_lunch.message

    # A failed lunch source never holds back the digest, it just has no line.
    failed = replace(lunch, status="error", dishes=())
    [on_time] = _digest([_school_plan()], lunch=failed)
    assert "Lunch" not in on_time.message


def test_digest_leaves_lunch_out_when_off_or_without_a_school_day() -> None:
    lunch = LunchMenu(
        local_date=DAY, status="ok", dishes=("Pasta",), fetched_at=_utc(DAY, 6, 0)
    )
    [off] = _digest(
        [_school_plan()], lunch=lunch, profile=_profile(lunch_in_digest=False)
    )
    assert "Lunch" not in off.message
    assert off.lunch_menu is None

    [no_school] = _digest([_extra_plan(_utc(DAY, 16, 55))], lunch=lunch)
    assert "Lunch" not in no_school.message
    assert no_school.lunch_menu is None


def test_digest_is_packing_only_when_evening_notice_is_off() -> None:
    profile = _profile(evening_notice_enabled=False)
    [digest] = _digest([_school_plan()], items=("Gympakläder",), profile=profile)
    assert digest.kind == KIND_PACKING
    assert digest.message == "Imorgon: packa Gympakläder."
    assert _digest([_school_plan()], profile=profile) == []


def test_digest_needs_a_mission_and_notifications() -> None:
    assert _digest([]) == []
    assert (
        _digest([_school_plan()], profile=_profile(notifications_enabled=False)) == []
    )


def test_digest_is_held_inside_quiet_hours_and_before_twenty() -> None:
    plans = [_school_plan()]
    assert _digest(plans, now=_utc(date(2026, 10, 19), 19, 59)) == []
    quiet = _profile(quiet_start=time(20, 0), quiet_end=time(6, 0))
    assert _digest(plans, profile=quiet) == []
    # Never on the day itself.
    assert _digest(plans, now=_utc(DAY, 20, 30)) == []


def test_digest_is_sent_once() -> None:
    sent = {
        KIND_EVENING: NotifiedRecord(
            kind=KIND_EVENING, sent_at=EVENING, leave_time=None, revision=1
        )
    }
    assert _digest([_school_plan()], state=_digest_state(sent)) == []


def test_digest_is_not_resent_after_an_upgrade_when_legacy_morning_has_it() -> None:
    legacy = _state(
        notified={
            KIND_EVENING: NotifiedRecord(
                kind=KIND_EVENING, sent_at=EVENING, leave_time=None, revision=1
            )
        }
    )
    assert _digest([_school_plan()], legacy=legacy) == []
    # An unrelated record on the legacy mission does not suppress it.
    other = _state(
        notified={
            KIND_MORNING: NotifiedRecord(
                kind=KIND_MORNING, sent_at=EVENING, leave_time=None, revision=1
            )
        }
    )
    assert len(_digest([_school_plan()], legacy=other)) == 1


def test_packing_absent_after_acknowledgement() -> None:
    """Acknowledged items are not in packing.items, so the morning notice omits them."""
    rec = _utc(DAY, 7, 32)
    morning_at = _utc(DAY, 6, 32)
    # Packing list already acknowledged → no pending items.
    intents = evaluate(
        None,
        _plan(recommended=rec),
        _state(),
        _packing(items=(), acknowledged=("Gympakläder",)),
        _profile(),
        now=morning_at,
    )
    morning = next(i for i in intents if i.kind == KIND_MORNING)
    assert morning.packing_items == ()
    assert "Gympakläder" not in morning.message


# --- Change alerts ---------------------------------------------------------


def test_small_changes_summing_past_threshold_alert() -> None:
    """Several small drifts vs the last notified leave sum past 3 min (§12.1)."""
    # Baseline: evening told the user 07:30.
    baseline = _utc(DAY, 7, 30)
    state = _state(
        first_published_leave=baseline,
        notified={
            KIND_EVENING: NotifiedRecord(
                kind=KIND_EVENING,
                sent_at=_utc(date(2026, 10, 19), 20, 0),
                leave_time=baseline,
                revision=1,
            )
        },
    )
    # Current plan now recommends 07:34 (4 min later) — a worsening.
    current = _plan(recommended=_utc(DAY, 7, 34), revision=2)
    now = _utc(DAY, 6, 0)
    intents = evaluate(None, current, state, _packing(), _profile(), now=now)
    change = next(i for i in intents if i.kind == KIND_CHANGE)
    assert REASON_WORSENING in change.reason_codes


def test_change_below_threshold_is_silent() -> None:
    baseline = _utc(DAY, 7, 30)
    state = _state(
        first_published_leave=baseline,
        notified={
            KIND_EVENING: NotifiedRecord(
                kind=KIND_EVENING,
                sent_at=_utc(date(2026, 10, 19), 20, 0),
                leave_time=baseline,
                revision=1,
            )
        },
    )
    # 2 minutes later — under the 3-minute threshold.
    current = _plan(recommended=_utc(DAY, 7, 32), revision=2)
    now = _utc(DAY, 6, 0)
    intents = evaluate(None, current, state, _packing(), _profile(), now=now)
    assert KIND_CHANGE not in _kinds(intents)


def test_improvement_requires_two_stable_revisions() -> None:
    """A plan moving earlier only alerts when stable across two revisions."""
    baseline = _utc(DAY, 7, 40)
    state = _state(
        first_published_leave=baseline,
        notified={
            KIND_EVENING: NotifiedRecord(
                kind=KIND_EVENING,
                sent_at=_utc(date(2026, 10, 19), 20, 0),
                leave_time=baseline,
                revision=1,
            )
        },
    )
    now = _utc(DAY, 6, 0)
    earlier = _utc(DAY, 7, 30)  # 10 min earlier
    current = _plan(recommended=earlier, revision=2)

    # First time we see the improvement, previous plan still had the old time:
    # not stable yet, no alert.
    previous_old = _plan(recommended=baseline, revision=1)
    first = evaluate(previous_old, current, state, _packing(), _profile(), now=now)
    assert KIND_CHANGE not in _kinds(first)

    # Second consecutive revision proposing the same earlier time → alert.
    previous_same = _plan(recommended=earlier, revision=1)
    second = evaluate(previous_same, current, state, _packing(), _profile(), now=now)
    change = next(i for i in second if i.kind == KIND_CHANGE)
    assert REASON_IMPROVEMENT in change.reason_codes


def test_change_cooldown_blocks_resend() -> None:
    """A worsening within 5 min of the last change notice is suppressed."""
    baseline = _utc(DAY, 7, 30)
    now = _utc(DAY, 6, 0)
    state = _state(
        first_published_leave=baseline,
        notified={
            KIND_CHANGE: NotifiedRecord(
                kind=KIND_CHANGE,
                sent_at=_utc(DAY, 5, 58),  # 2 min ago
                leave_time=_utc(DAY, 7, 34),
                revision=2,
            )
        },
    )
    # Now 07:38 (worse again) but cooldown not elapsed.
    current = _plan(recommended=_utc(DAY, 7, 38), revision=3)
    intents = evaluate(None, current, state, _packing(), _profile(), now=now)
    assert KIND_CHANGE not in _kinds(intents)


def test_plan_moving_back_does_not_resend_same_step() -> None:
    """A plan recomputed to the same already-notified leave resends nothing."""
    rec = _utc(DAY, 7, 32)
    state = _state(
        notified={
            KIND_MORNING: NotifiedRecord(
                kind=KIND_MORNING, sent_at=_utc(DAY, 6, 32), leave_time=rec, revision=1
            ),
            KIND_REMINDER: NotifiedRecord(
                kind=KIND_REMINDER, sent_at=_utc(DAY, 7, 22), leave_time=rec, revision=1
            ),
        }
    )
    # A later revision with the identical recommended time, still before rec.
    current = _plan(recommended=rec, revision=2)
    now = _utc(DAY, 7, 25)
    intents = evaluate(None, current, state, _packing(), _profile(), now=now)
    assert KIND_MORNING not in _kinds(intents)
    assert KIND_REMINDER not in _kinds(intents)
    assert KIND_CHANGE not in _kinds(intents)


# --- Critical --------------------------------------------------------------


def test_critical_fires_when_cannot_arrive_on_time() -> None:
    rec = _utc(DAY, 7, 32)
    current = _plan(
        recommended=rec,
        status="cannot_arrive_on_time",
        feasible=False,
        reason_codes=("journey_leg_cancelled",),
    )
    now = _utc(DAY, 6, 0)
    intents = evaluate(None, current, _state(), _packing(), _profile(), now=now)
    assert KIND_CRITICAL in _kinds(intents)


# --- Dedup and tag ---------------------------------------------------------


def test_dedup_keyed_on_mission_and_kind() -> None:
    """Re-evaluating after a kind is recorded produces no duplicate (§11.3)."""
    rec = _utc(DAY, 7, 32)
    morning_at = _utc(DAY, 6, 32)
    state = _state(
        notified={
            KIND_MORNING: NotifiedRecord(
                kind=KIND_MORNING, sent_at=morning_at, leave_time=rec, revision=1
            )
        }
    )
    intents = evaluate(
        None, _plan(recommended=rec), state, _packing(), _profile(), now=morning_at
    )
    assert KIND_MORNING not in _kinds(intents)


def test_tag_is_stable_per_mission() -> None:
    rec = _utc(DAY, 7, 32)
    intents = evaluate(
        None, _plan(recommended=rec), _state(), _packing(), _profile(), now=rec
    )
    assert intents
    for intent in intents:
        assert intent.tag == f"departure_{MISSION}"


def test_intent_channels_come_from_profile_scripts() -> None:
    rec = _utc(DAY, 7, 32)
    profile = _profile(scripts={"push": "script.p", "live_update": "script.lu"})
    intents = evaluate(
        None, _plan(recommended=rec), _state(), _packing(), profile, now=rec
    )
    leave_now = next(i for i in intents if i.kind == KIND_LEAVE_NOW)
    assert set(leave_now.channels) == {"push", "live_update"}


# --- Extra-link missions (plan item 6) --------------------------------------

EXTRA_MISSION = "kid_b:2026-10-20:x-club-k1"


def _extra_plan(recommended: datetime) -> DeparturePlan:
    start = _utc(DAY, 17, 30)
    req = ArrivalRequirement(
        mission_id=EXTRA_MISSION,
        person_id="kid_b",
        local_date=DAY,
        event_id="f1",
        event_start=start,
        arrival_deadline=start,
        destination_id="pitch",
        source="club",
        slot="x-club-k1",
        link_id="club",
        label="Fotboll",
        destination_name="Planen",
    )
    return replace(
        _plan(recommended=recommended, requirement=req), mission_id=EXTRA_MISSION
    )


def test_extra_mission_title_names_activity_and_destination() -> None:
    rec = _utc(DAY, 17, 0)
    intents = evaluate(
        None,
        _extra_plan(rec),
        replace(_state(), mission_id=EXTRA_MISSION),
        _packing(items=("Gympakläder",)),
        _profile(),
        now=rec,
    )
    leave_now = next(i for i in intents if i.kind == KIND_LEAVE_NOW)
    assert leave_now.title == "Kid B – Fotboll 17:30"
    assert leave_now.message.endswith("→ Planen")
    assert leave_now.slot == "x-club-k1"
    assert leave_now.tag == f"departure_{EXTRA_MISSION}"


def test_extra_mission_notices_carry_the_packing_list() -> None:
    """Packing rides on every mission's morning notice until acknowledged (§6.1)."""
    rec = _utc(DAY, 17, 0)
    intents = evaluate(
        None,
        _extra_plan(rec),
        replace(_state(), mission_id=EXTRA_MISSION),
        _packing(items=("Gympakläder",)),
        _profile(),
        now=rec - timedelta(minutes=50),
    )
    morning = next(i for i in intents if i.kind == KIND_MORNING)
    assert morning.packing_items == ("Gympakläder",)


def test_leave_message_names_a_non_default_destination_only() -> None:
    rec = _utc(DAY, 17, 0)
    profile = _profile()
    extra = _extra_plan(rec)
    [leave_now] = [
        i
        for i in evaluate(
            None,
            extra,
            replace(_state(), mission_id=EXTRA_MISSION),
            _packing(),
            profile,
            now=rec,
        )
        if i.kind == KIND_LEAVE_NOW
    ]
    assert leave_now.message.endswith(" → Planen")
    # At the profile's own default location nothing is appended.
    at_default = replace(
        extra,
        requirement=replace(extra.requirement, destination_id="school"),
    )
    [again] = [
        i
        for i in evaluate(
            None,
            at_default,
            replace(_state(), mission_id=EXTRA_MISSION),
            _packing(),
            profile,
            now=rec,
        )
        if i.kind == KIND_LEAVE_NOW
    ]
    assert "→" not in again.message


def _told(*kinds: str) -> MissionState:
    sent = _utc(DAY, 16, 0)
    return replace(
        _state(),
        mission_id=EXTRA_MISSION,
        notified={
            kind: NotifiedRecord(kind=kind, sent_at=sent, leave_time=sent, revision=1)
            for kind in kinds
        },
    )


def test_removed_event_sends_removed_and_cleanup_after_a_leave_notice() -> None:
    plan = _extra_plan(_utc(DAY, 17, 0))
    intents = evaluate_removed(plan, _told(KIND_MORNING), _profile(), _utc(DAY, 16, 30))
    # Cleanup first: it shares the tag and would otherwise clear the notice.
    assert [i.kind for i in intents] == [KIND_CLEANUP, KIND_REMOVED]
    assert intents[1].message == "Fotboll 17:30 är borttagen ur kalendern."


def test_removed_event_is_silent_when_nobody_was_told() -> None:
    plan = _extra_plan(_utc(DAY, 17, 0))
    assert evaluate_removed(plan, _told(), _profile(), _utc(DAY, 16, 30)) == []
    # Only evening-type or leave_now records are not leave notices either.
    assert (
        evaluate_removed(plan, _told(KIND_LEAVE_NOW), _profile(), _utc(DAY, 16, 30))
        == []
    )


def test_removed_event_is_sent_once_and_never_after_the_event_started() -> None:
    plan = _extra_plan(_utc(DAY, 17, 0))
    profile = _profile()
    assert (
        evaluate_removed(
            plan, _told(KIND_MORNING, KIND_REMOVED), profile, _utc(DAY, 16, 30)
        )
        == []
    )
    assert evaluate_removed(plan, _told(KIND_MORNING), profile, _utc(DAY, 17, 30)) == []
    assert (
        evaluate_removed(
            plan,
            _told(KIND_MORNING),
            _profile(notifications_enabled=False),
            _utc(DAY, 16, 30),
        )
        == []
    )


def test_removed_event_skips_cleanup_already_sent() -> None:
    plan = _extra_plan(_utc(DAY, 17, 0))
    intents = evaluate_removed(
        plan, _told(KIND_MORNING, KIND_CLEANUP), _profile(), _utc(DAY, 16, 30)
    )
    assert [i.kind for i in intents] == [KIND_REMOVED]
