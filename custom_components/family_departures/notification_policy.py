"""Notification policy for the Family Departures integration (spec §12.1).

:func:`evaluate` is a pure function (plan §1 rule 3): no ``homeassistant``
import, ``now`` is a parameter and the clock is never read here. It decides
*which* notifications a mission needs right now and returns a list of
:class:`NotificationIntent`; the scheduler/dispatcher (T17/T18) own timing and
delivery. The function is idempotent for a given ``(now, state)``: already-sent
notifications are suppressed via the ``(mission_id, kind)`` ledger in
``state.notified`` so re-evaluating the same minute produces nothing new.

Decisions implemented (spec §12.1, §5.5):

* Kinds, in the order they fire over a mission's life: ``morning`` (−60 min),
  ``reminder`` (−10 min), ``leave_now`` (recommended departure passed),
  ``change`` (≥ threshold vs the last notified leave), ``critical``
  (cancelled/infeasible trip) and ``cleanup`` (departure confirmed → clear Live Update).
* Change alerts compare against the **last notified** leave time, or the
  ``first_published_leave`` baseline when nothing has been sent yet, so several
  small drifts that sum past the threshold are not lost. A worsening (later
  home departure) alerts immediately; an improvement (earlier departure) must
  be stable across two consecutive revisions and never moves the departure
  later unsafely. A 5-minute cooldown applies to ``change``; a ``critical``
  change bypasses it.
* Quiet hours (``profile.quiet_start``–``profile.quiet_end``): a ``morning``
  notice landing inside them is held until quiet-end, or dropped when the
  recommended departure would then be under 15 minutes away. ``reminder``,
  ``leave_now`` and ``critical`` for today's active trip always pass. Evening
  and next-day notices are held inside quiet hours.
* Deduplication is keyed on ``(mission_id, kind)``; the Android ``tag`` is
  stable per mission (``departure_<mission_id>``) so a replacement notification
  overwrites the previous one on the phone instead of stacking (§11.3).
* The 20:00 ``evening`` (or ``packing``, when the evening notice is off) notice
  is one digest per person and day, :func:`evaluate_day_digest`, listing every
  mission of tomorrow plus the packing list (plan §6.2).
* Packing items ride on the main mission's ``morning`` and ``reminder`` notices,
  taken from ``packing.items`` (already the still-pending items, §5.5).
* No intents are produced once the mission is ``departed``/``skipped`` other
  than a one-off ``cleanup`` to clear the Live Update.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta

from .models import (
    DeparturePlan,
    LunchMenu,
    MissionState,
    NotificationIntent,
    NotifiedRecord,
    PackingList,
    ProfileConfig,
    Quality,
)
from .timeutil import TZ

# Notification kinds (ledger keys and intent ``kind`` values, §12.1).
KIND_EVENING = "evening"
KIND_PACKING = "packing"
KIND_MORNING = "morning"
KIND_REMINDER = "reminder"
KIND_LEAVE_NOW = "leave_now"
KIND_CHANGE = "change"
KIND_CRITICAL = "critical"
KIND_CLEANUP = "cleanup"
KIND_REMOVED = "removed"

# The main (school) mission; only it carries the packing list on its notices.
MAIN_SLOT = "morning"
# The day-before digest is a mission of its own (``person:d:day``): it holds the
# ledger and the action nonce for the 20:00 evening/packing notice.
DIGEST_SLOT = "day"

# Severities carried to the channel scripts (§12.2).
_SEVERITY = {
    KIND_EVENING: "info",
    KIND_PACKING: "info",
    KIND_MORNING: "info",
    KIND_REMINDER: "info",
    KIND_LEAVE_NOW: "warning",
    KIND_CHANGE: "info",
    KIND_CRITICAL: "warning",
    KIND_CLEANUP: "info",
    KIND_REMOVED: "info",
}

# Timing windows relative to the recommended departure (§12.1).
MORNING_LEAD_MINUTES = 60
REMINDER_LEAD_MINUTES = 10
EVENING_HOUR = 20

# A morning notice shifted to quiet-end is dropped if less than this many
# minutes remain before the recommended departure (§12.1).
MIN_MINUTES_AFTER_QUIET = 15

# Change-alert cooldown; a critical change bypasses it (§12.1).
CHANGE_COOLDOWN_MINUTES = 5

# The change threshold is per-profile (``change_threshold_minutes``, default 3
# per the §12.1 table): a home departure moving later by at least that many
# minutes is a worsening, the same magnitude earlier is an improvement.

# Reason codes attached to change/critical intents so the dashboard can explain
# them without re-deriving the comparison.
REASON_WORSENING = "leave_time_worsened"
REASON_IMPROVEMENT = "leave_time_improved"
REASON_CANCELLED = "trip_cancelled"
REASON_CANNOT_ARRIVE = "cannot_arrive_on_time"


def _title(profile: ProfileConfig, plan: DeparturePlan, phrase: str) -> str:
    """Notification title: the person, then the phrase.

    An extra-link mission is titled by its activity and start time, so two
    missions of one person are distinguishable on the phone: ``Kid – Fotboll
    17:30`` (plan §6.1); the kind is told by the message. The main mission keeps
    ``Kid – <phrase>``.
    """
    req = plan.requirement
    if req.slot == MAIN_SLOT:
        return f"{profile.name} – {phrase}"
    subject = req.label or "aktivitet"
    return f"{profile.name} – {subject} {_hhmm(req.event_start)}"


def _tag(mission_id: str) -> str:
    """Stable Android notification tag per mission (§11.3, §12.3)."""
    return f"departure_{mission_id}"


def in_quiet_hours(now: datetime, quiet_start: time, quiet_end: time) -> bool:
    """True if the local wall-clock ``now`` is inside the quiet window.

    Quiet hours normally wrap midnight (e.g. 21:00–06:00). An empty window
    (start == end) is treated as "never quiet".
    """
    local = now.astimezone(TZ).time()
    if quiet_start == quiet_end:
        return False
    if quiet_start < quiet_end:
        return quiet_start <= local < quiet_end
    # Wraps midnight: inside if after start or before end.
    return local >= quiet_start or local < quiet_end


def _last_notified(state: MissionState, kind: str) -> NotifiedRecord | None:
    """The ledger entry for ``kind`` on this mission, if any."""
    return state.notified.get(kind)


def _already_sent(state: MissionState, kind: str) -> bool:
    """True if a notification of ``kind`` has already been recorded (§11.3)."""
    return kind in state.notified


def _baseline_leave(state: MissionState) -> datetime | None:
    """Leave time to compare a change against (§12.1).

    Uses the most recent *leave-bearing* notified record (the last time the
    user was told a departure time), falling back to the first published
    revision's leave time when nothing has been sent yet. This makes several
    small drifts cumulative rather than resetting the baseline each revision.
    """
    latest: NotifiedRecord | None = None
    for record in state.notified.values():
        if record.leave_time is None:
            continue
        if latest is None or record.sent_at > latest.sent_at:
            latest = record
    if latest is not None:
        return latest.leave_time
    return state.first_published_leave


def _packing_items(packing: PackingList) -> tuple[str, ...]:
    """Still-pending packing items for the mission's date (§5.5)."""
    return packing.items


def _quality_label(quality: Quality) -> str:
    """Short Swedish label for the plan's data quality (§12.1)."""
    return {
        "realtime": "realtid",
        "scheduled": "tidtabell",
        "estimated": "uppskattat",
        "stale": "inaktuellt",
        "unavailable": "okänt",
    }[quality]


def _build_intent(
    *,
    plan: DeparturePlan,
    profile: ProfileConfig,
    state: MissionState,
    kind: str,
    title: str,
    message: str,
    packing_items: tuple[str, ...] = (),
    reason_codes: tuple[str, ...] = (),
) -> NotificationIntent:
    """Assemble a :class:`NotificationIntent` for one mission and kind."""
    return NotificationIntent(
        person_id=profile.id,
        mission_id=plan.mission_id,
        plan_id=plan.plan_id,
        revision=plan.revision,
        notification_id=f"{plan.mission_id}:{kind}",
        kind=kind,
        severity=_SEVERITY[kind],
        title=title,
        message=message,
        recommended_leave_time=plan.recommended_leave,
        latest_leave_time=plan.latest_leave,
        quality=plan.quality,
        reason_codes=reason_codes,
        packing_items=packing_items,
        channels=tuple(profile.scripts.keys()),
        tag=_tag(plan.mission_id),
        action_nonce=state.action_nonce,
        slot=plan.requirement.slot,
    )


def _leave_message(plan: DeparturePlan, profile: ProfileConfig) -> str:
    """Message body for morning/reminder/leave-now (§12.1).

    Appends ``→ <destination>`` when the mission goes somewhere other than the
    profile's default location (plan §6.1).
    """
    if plan.recommended_leave is None:
        return "Ingen säker avgångstid – kontrollera planen."
    rec = plan.recommended_leave.astimezone(TZ).strftime("%H:%M")
    quality = _quality_label(plan.quality)
    if plan.latest_leave is not None:
        latest = plan.latest_leave.astimezone(TZ).strftime("%H:%M")
        message = f"Rekommenderad avgång {rec}, senast {latest} ({quality})."
    else:
        message = f"Rekommenderad avgång {rec} ({quality})."
    req = plan.requirement
    if req.destination_name and req.destination_id != profile.default_location_id:
        message += f" → {req.destination_name}"
    return message


def _is_closed(state: MissionState, plan: DeparturePlan) -> bool:
    """True when the mission is finished and should produce no more trips."""
    return state.status in ("departed", "skipped") or plan.status in (
        "departed",
        "skipped",
    )


def _is_critical(plan: DeparturePlan) -> tuple[bool, tuple[str, ...]]:
    """Whether the current plan is a critical travel change, with reasons.

    A trip that cannot arrive on time, or whose chosen journey leg is
    cancelled, is critical (§12.1 "Kritisk reseändring").
    """
    reasons: list[str] = []
    if plan.status == "cannot_arrive_on_time":
        reasons.append(REASON_CANNOT_ARRIVE)
    if "journey_leg_cancelled" in plan.reason_codes:
        reasons.append(REASON_CANCELLED)
    return (bool(reasons), tuple(reasons))


def evaluate(
    previous: DeparturePlan | None,
    current: DeparturePlan,
    state: MissionState,
    packing: PackingList,
    profile: ProfileConfig,
    now: datetime,
) -> list[NotificationIntent]:
    """Decide which notifications ``current`` needs right now (spec §12.1).

    Pure and idempotent: with the same ``now`` and ``state`` it yields the same
    intents, and anything already recorded in ``state.notified`` is suppressed.
    The caller records a :class:`NotifiedRecord` per returned intent before the
    next evaluation so dedup works across ticks and restarts (§11.3).
    """
    if not profile.notifications_enabled:
        return []

    intents: list[NotificationIntent] = []

    # --- Cleanup wins: a confirmed departure clears the Live Update once. ----
    if _is_closed(state, current):
        if not _already_sent(state, KIND_CLEANUP):
            intents.append(
                _build_intent(
                    plan=current,
                    profile=profile,
                    state=state,
                    kind=KIND_CLEANUP,
                    title=_title(profile, current, "klar"),
                    message="Avresa bekräftad. Rensar påminnelser.",
                )
            )
        return intents

    rec = current.recommended_leave
    local_date = current.requirement.local_date
    today = now.astimezone(TZ).date()
    is_today = local_date == today
    quiet = in_quiet_hours(now, profile.quiet_start, profile.quiet_end)
    packing_items = _packing_items(packing)

    # --- Critical travel change (bypasses cooldown, passes quiet for today). -
    critical, critical_reasons = _is_critical(current)
    if critical and not _already_sent(state, KIND_CRITICAL):
        # Next-day criticals are held inside quiet hours; today's active trip
        # always passes (§12.1).
        if is_today or not quiet:
            intents.append(
                _build_intent(
                    plan=current,
                    profile=profile,
                    state=state,
                    kind=KIND_CRITICAL,
                    title=_title(profile, current, "reseändring"),
                    message="Vald resa fungerar inte – se nästa nåbara alternativ.",
                    reason_codes=critical_reasons,
                )
            )

    # (The evening / packing notices moved to :func:`evaluate_day_digest`.)

    # --- Timed morning / reminder / leave-now (relative to recommended). -----
    if rec is not None and is_today:
        morning_at = rec - timedelta(minutes=MORNING_LEAD_MINUTES)
        reminder_at = rec - timedelta(minutes=REMINDER_LEAD_MINUTES)

        # Morning (−60): subject to quiet hours shifting/skip.
        if now >= morning_at and not _already_sent(state, KIND_MORNING):
            if _morning_passes_quiet(now, rec, profile):
                intents.append(
                    _build_intent(
                        plan=current,
                        profile=profile,
                        state=state,
                        kind=KIND_MORNING,
                        title=_title(profile, current, "dagens avgång"),
                        message=_leave_message(current, profile),
                        packing_items=packing_items,
                    )
                )

        # Reminder (−10) and leave-now always pass quiet hours (active trip).
        if now >= reminder_at and now < rec and not _already_sent(state, KIND_REMINDER):
            intents.append(
                _build_intent(
                    plan=current,
                    profile=profile,
                    state=state,
                    kind=KIND_REMINDER,
                    title=_title(profile, current, "snart dags"),
                    message=_leave_message(current, profile),
                    packing_items=packing_items,
                )
            )

        if now >= rec and not _already_sent(state, KIND_LEAVE_NOW):
            intents.append(
                _build_intent(
                    plan=current,
                    profile=profile,
                    state=state,
                    kind=KIND_LEAVE_NOW,
                    title=_title(profile, current, "dags att gå"),
                    message=_leave_message(current, profile),
                )
            )

    # --- Change alert vs the last notified leave time. -----------------------
    change_intent = _maybe_change_intent(
        previous=previous,
        current=current,
        state=state,
        profile=profile,
        now=now,
        is_today=is_today,
        quiet=quiet,
    )
    if change_intent is not None:
        intents.append(change_intent)

    return intents


def _morning_passes_quiet(
    now: datetime, recommended: datetime, profile: ProfileConfig
) -> bool:
    """Whether a morning (−60) notice may be sent given quiet hours (§12.1).

    Inside quiet hours the morning notice is held (returns ``False``). Once
    ``now`` has reached quiet-end it is released, unless the recommended
    departure is then under :data:`MIN_MINUTES_AFTER_QUIET` minutes away, in
    which case it is skipped for good (the −10 reminder and leave-now cover the
    person instead). Outside quiet hours it is always sent.
    """
    if in_quiet_hours(now, profile.quiet_start, profile.quiet_end):
        # Still inside quiet hours: hold until quiet-end.
        return False
    # At or past quiet-end (or quiet hours never applied). If the natural
    # morning time (recommended − 60) fell inside quiet hours and the departure
    # is now under the floor, skip it entirely; the −10 reminder and leave-now
    # cover the person instead (§12.1).
    natural_morning = recommended - timedelta(minutes=MORNING_LEAD_MINUTES)
    was_held = in_quiet_hours(natural_morning, profile.quiet_start, profile.quiet_end)
    minutes_left = (recommended - now).total_seconds() / 60
    if was_held and minutes_left < MIN_MINUTES_AFTER_QUIET:
        return False
    return True


def _maybe_change_intent(
    *,
    previous: DeparturePlan | None,
    current: DeparturePlan,
    state: MissionState,
    profile: ProfileConfig,
    now: datetime,
    is_today: bool,
    quiet: bool,
) -> NotificationIntent | None:
    """Build a ``change`` intent when the leave time moved enough (§12.1).

    Compares the current recommended leave against the baseline (last notified
    leave, else ``first_published_leave``). A worsening of at least
    ``profile.change_threshold_minutes`` alerts immediately; an improvement of
    the same size must be stable across two consecutive revisions and is never
    allowed to move the home departure later. A 5-minute cooldown applies.
    Next-day changes are held inside quiet hours.
    """
    rec = current.recommended_leave
    if rec is None:
        return None

    baseline = _baseline_leave(state)
    if baseline is None:
        return None

    delta_minutes = (rec - baseline).total_seconds() / 60
    threshold = profile.change_threshold_minutes
    if abs(delta_minutes) < threshold:
        return None

    # Next-day change is held inside quiet hours; today's active trip passes.
    if quiet and not is_today:
        return None

    # Cooldown vs the last change notification.
    last_change = _last_notified(state, KIND_CHANGE)
    if last_change is not None:
        since = (now - last_change.sent_at).total_seconds() / 60
        if since < CHANGE_COOLDOWN_MINUTES:
            return None
        # Don't re-send the same baseline move: require movement vs the last
        # notified change too, otherwise the dedup is only the cooldown.
        if last_change.leave_time is not None and last_change.leave_time == rec:
            return None

    if delta_minutes > 0:
        # Worsening (later departure): alert immediately.
        return _build_intent(
            plan=current,
            profile=profile,
            state=state,
            kind=KIND_CHANGE,
            title=_title(profile, current, "ändrad avgång"),
            message=_leave_message(current, profile),
            reason_codes=(REASON_WORSENING,),
        )

    # Improvement (earlier departure): must be stable across two consecutive
    # revisions and never moves the departure later (it is earlier by
    # construction here). Requires a previous plan proposing the same earlier
    # time (§12.1 "förbättring måste vara stabil i två uppdateringar").
    if previous is None or previous.recommended_leave is None:
        return None
    if previous.recommended_leave != rec:
        return None
    return _build_intent(
        plan=current,
        profile=profile,
        state=state,
        kind=KIND_CHANGE,
        title=_title(profile, current, "ändrad avgång"),
        message=_leave_message(current, profile),
        reason_codes=(REASON_IMPROVEMENT,),
    )


def evaluate_removed(
    plan: DeparturePlan,
    state: MissionState,
    profile: ProfileConfig,
    now: datetime,
) -> list[NotificationIntent]:
    """Notices for an extra mission whose event vanished from the calendar (§4.4).

    ``plan`` is the last published plan of the vanished mission. Silent unless
    the user was already told a departure (a ``morning``, ``reminder`` or
    ``change`` notice in the ledger), the event has not started yet and no
    ``removed`` notice went out. Then it yields one ``removed`` notice plus a
    ``cleanup`` that clears the tagged notification. The cleanup comes first:
    both share the mission's tag, so clearing afterwards would wipe the
    ``removed`` notice from the phone the moment it appeared.
    """
    if not profile.notifications_enabled:
        return []
    if plan.requirement.slot == MAIN_SLOT:
        return []
    if _already_sent(state, KIND_REMOVED) or _is_closed(state, plan):
        return []
    if now >= plan.requirement.event_start:
        return []
    if not any(
        kind in state.notified for kind in (KIND_MORNING, KIND_REMINDER, KIND_CHANGE)
    ):
        return []

    req = plan.requirement
    subject = req.label or "aktivitet"
    when = req.event_start.astimezone(TZ).strftime("%H:%M")
    intents: list[NotificationIntent] = []
    if not _already_sent(state, KIND_CLEANUP):
        intents.append(
            _build_intent(
                plan=plan,
                profile=profile,
                state=state,
                kind=KIND_CLEANUP,
                title=_title(profile, plan, "klar"),
                message="Händelsen är borttagen. Rensar påminnelser.",
            )
        )
    intents.append(
        _build_intent(
            plan=plan,
            profile=profile,
            state=state,
            kind=KIND_REMOVED,
            title=_title(profile, plan, "borttagen"),
            message=f"{subject} {when} är borttagen ur kalendern.",
        )
    )
    return intents


# --- Day-before digest (plan §6.2) -------------------------------------------

_WARNING_TEXT = {
    "home_gap_too_short": "⚠ kort tid hemma",
    "overlaps_other_commitment": "⚠ krockar med annat",
}


def _hhmm(value: datetime) -> str:
    return value.astimezone(TZ).strftime("%H:%M")


def _digest_line(plan: DeparturePlan) -> str:
    """One mission as a digest line: ``17:30 Fotboll → Planen – gå 16:55 ⚠ …``."""
    req = plan.requirement
    label = req.label or ("Skola" if req.slot == MAIN_SLOT else "Aktivitet")
    if req.slot != MAIN_SLOT and req.destination_name:
        label = f"{label} → {req.destination_name}"
    if plan.recommended_leave is not None:
        line = f"{_hhmm(req.event_start)} {label} – gå {_hhmm(plan.recommended_leave)}"
    else:
        line = f"{_hhmm(req.event_start)} {label} – ingen säker avgångstid"
    marks = [_WARNING_TEXT[w] for w in req.warnings if w in _WARNING_TEXT]
    return " ".join([line, *marks])


def _lunch_text(
    lunch: LunchMenu | None, profile: ProfileConfig, plans: Sequence[DeparturePlan]
) -> str | None:
    """The digest's lunch line, only for a school day (a main mission) with a menu."""
    if lunch is None or lunch.status != "ok" or not lunch.dishes:
        return None
    if not profile.lunch_in_digest:
        return None
    if not any(plan.requirement.slot == MAIN_SLOT for plan in plans):
        return None
    return "Lunch imorgon: " + ", ".join(lunch.dishes)


def evaluate_day_digest(
    profile: ProfileConfig,
    d: date,
    plans: Sequence[DeparturePlan],
    packing: PackingList,
    lunch: LunchMenu | None,
    state: MissionState,
    legacy_state: MissionState | None,
    now: datetime,
) -> list[NotificationIntent]:
    """The 20:00 day-before digest for ``d`` (plan §6.2); empty when not due.

    One ``evening`` notice (or ``packing`` when the evening notice is off and
    there is something to pack) listing every mission of ``d`` in start order,
    then the packing list and the lunch. Held inside quiet hours, never sent on
    or after ``d`` itself, and never when ``d`` has no mission. ``state`` is the
    ``person:d:day`` mission (ledger and action nonce); a ``legacy_state`` (the
    old ``person:d:morning``) that already carries an ``evening``/``packing``
    record counts as sent, so an upgrade between 20:00 and midnight does not
    send a second digest.
    """
    if not profile.notifications_enabled or not plans:
        return []
    local_now = now.astimezone(TZ)
    if local_now.date() >= d or local_now.hour < EVENING_HOUR:
        return []
    if in_quiet_hours(now, profile.quiet_start, profile.quiet_end):
        return []
    digest_kinds = (KIND_EVENING, KIND_PACKING)
    if any(kind in state.notified for kind in digest_kinds):
        return []
    if legacy_state is not None and any(
        kind in legacy_state.notified for kind in digest_kinds
    ):
        return []

    items = packing.items
    if profile.evening_notice_enabled:
        kind = KIND_EVENING
        title = f"{profile.name} – imorgon"
    elif items:
        kind = KIND_PACKING
        title = f"{profile.name} – packa inför imorgon"
    else:
        return []

    ordered = sorted(plans, key=lambda p: p.requirement.event_start)
    lunch_text = _lunch_text(lunch, profile, ordered)
    if kind == KIND_EVENING:
        parts = [_digest_line(plan) for plan in ordered]
        if items:
            parts.append("Packa: " + ", ".join(items))
        if lunch_text is not None:
            parts.append(lunch_text)
        message = "\n".join(parts)
    else:
        message = "Imorgon: packa " + ", ".join(items) + "."

    with_leave = [p for p in ordered if p.recommended_leave is not None]
    first = with_leave[0] if with_leave else ordered[0]
    revision = max(plan.revision for plan in ordered)
    return [
        NotificationIntent(
            person_id=profile.id,
            mission_id=state.mission_id,
            plan_id=f"{state.mission_id}:{revision}",
            revision=revision,
            notification_id=f"{state.mission_id}:{kind}",
            kind=kind,
            severity=_SEVERITY[kind],
            title=title,
            message=message,
            recommended_leave_time=first.recommended_leave,
            latest_leave_time=first.latest_leave,
            quality=first.quality,
            reason_codes=(),
            packing_items=items,
            channels=tuple(profile.scripts.keys()),
            tag=_tag(state.mission_id),
            action_nonce=state.action_nonce,
            lunch_menu=lunch_text if kind == KIND_EVENING else None,
            slot=DIGEST_SLOT,
        )
    ]
