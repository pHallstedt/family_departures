"""Notification policy for the Family Departures integration (spec §12.1).

:func:`evaluate` is a pure function (plan §1 rule 3): no ``homeassistant``
import, ``now`` is a parameter and the clock is never read here. It decides
*which* notifications a mission needs right now and returns a list of
:class:`NotificationIntent`; the scheduler/dispatcher (T17/T18) own timing and
delivery. The function is idempotent for a given ``(now, state)``: already-sent
notifications are suppressed via the ``(mission_id, kind)`` ledger in
``state.notified`` so re-evaluating the same minute produces nothing new.

Decisions implemented (spec §12.1, §5.5):

* Kinds, in the order they fire over a mission's life: ``evening`` (20:00, with
  tomorrow's packing list), ``packing`` (20:00, only when the evening notice is
  off for the profile), ``morning`` (−60 min), ``reminder`` (−10 min),
  ``leave_now`` (recommended departure passed), ``change`` (≥ threshold vs the
  last notified leave), ``critical`` (cancelled/infeasible trip) and
  ``cleanup`` (departure confirmed → clear Live Update).
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
* Packing items ride on the ``evening``, ``morning`` and ``reminder`` notices,
  taken from ``packing.items`` (already the still-pending items, §5.5).
* No intents are produced once the mission is ``departed``/``skipped`` other
  than a one-off ``cleanup`` to clear the Live Update.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from .models import (
    DeparturePlan,
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


def _tag(mission_id: str) -> str:
    """Stable Android notification tag per mission (§11.3, §12.3)."""
    return f"departure_{mission_id}"


def _in_quiet_hours(now: datetime, quiet_start: time, quiet_end: time) -> bool:
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
    )


def _evening_message(plan: DeparturePlan, items: tuple[str, ...]) -> str:
    """Preliminary evening message; never claims live traffic (§12.1)."""
    base = "Morgondagens plan är preliminär."
    if plan.recommended_leave is not None:
        local = plan.recommended_leave.astimezone(TZ).strftime("%H:%M")
        base = f"Imorgon: rekommenderad avgång {local} (preliminärt)."
    if items:
        return "Imorgon: packa " + ", ".join(items) + f". {base}"
    return base


def _leave_message(plan: DeparturePlan) -> str:
    """Message body for morning/reminder/leave-now (§12.1)."""
    if plan.recommended_leave is None:
        return "Ingen säker avgångstid – kontrollera planen."
    rec = plan.recommended_leave.astimezone(TZ).strftime("%H:%M")
    quality = _quality_label(plan.quality)
    if plan.latest_leave is not None:
        latest = plan.latest_leave.astimezone(TZ).strftime("%H:%M")
        return f"Rekommenderad avgång {rec}, senast {latest} ({quality})."
    return f"Rekommenderad avgång {rec} ({quality})."


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
                    title=f"{profile.name} – klar",
                    message="Avresa bekräftad. Rensar påminnelser.",
                )
            )
        return intents

    rec = current.recommended_leave
    local_date = current.requirement.local_date
    today = now.astimezone(TZ).date()
    is_today = local_date == today
    quiet = _in_quiet_hours(now, profile.quiet_start, profile.quiet_end)
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
                    title=f"{profile.name} – reseändring",
                    message="Vald resa fungerar inte – se nästa nåbara alternativ.",
                    reason_codes=critical_reasons,
                )
            )

    # --- Evening / packing-only notice (20:00 of the day before the trip). ---
    # Held inside quiet hours (next-day notice).
    local_now = now.astimezone(TZ)
    if (
        not is_today
        and local_now.hour >= EVENING_HOUR
        and not quiet
        and rec is not None
    ):
        if profile.evening_notice_enabled:
            # Full evening summary carries the packing list (§5.5, §12.1).
            if not _already_sent(state, KIND_EVENING):
                intents.append(
                    _build_intent(
                        plan=current,
                        profile=profile,
                        state=state,
                        kind=KIND_EVENING,
                        title=f"{profile.name} – imorgon",
                        message=_evening_message(current, packing_items),
                        packing_items=packing_items,
                    )
                )
        elif packing_items and not _already_sent(state, KIND_PACKING):
            # Evening summary off: send a packing-only notice at the same time,
            # but only when there is actually something to pack (§5.5, §12.1).
            intents.append(
                _build_intent(
                    plan=current,
                    profile=profile,
                    state=state,
                    kind=KIND_PACKING,
                    title=f"{profile.name} – packa inför imorgon",
                    message="Imorgon: packa " + ", ".join(packing_items) + ".",
                    packing_items=packing_items,
                )
            )

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
                        title=f"{profile.name} – dagens avgång",
                        message=_leave_message(current),
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
                    title=f"{profile.name} – snart dags",
                    message=_leave_message(current),
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
                    title=f"{profile.name} – dags att gå",
                    message=_leave_message(current),
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
    if _in_quiet_hours(now, profile.quiet_start, profile.quiet_end):
        # Still inside quiet hours: hold until quiet-end.
        return False
    # At or past quiet-end (or quiet hours never applied). If the natural
    # morning time (recommended − 60) fell inside quiet hours and the departure
    # is now under the floor, skip it entirely; the −10 reminder and leave-now
    # cover the person instead (§12.1).
    natural_morning = recommended - timedelta(minutes=MORNING_LEAD_MINUTES)
    was_held = _in_quiet_hours(natural_morning, profile.quiet_start, profile.quiet_end)
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
            title=f"{profile.name} – ändrad avgång",
            message=_leave_message(current),
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
        title=f"{profile.name} – ändrad avgång",
        message=_leave_message(current),
        reason_codes=(REASON_IMPROVEMENT,),
    )
