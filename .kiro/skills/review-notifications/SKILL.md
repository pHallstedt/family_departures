---
name: review-notifications
description: Review notification policy, scheduling and delivery - reminder timing, change thresholds, quiet hours, deduplication by mission_id, restart catch-up, dispatcher and channel scripts, Live Update, actionable buttons, presence-based departure and packing reminders. Use when reviewing notification_policy.py, scheduler.py, dispatcher.py, examples/scripts.yaml or presence handling.
---

# Review: notifications, scheduling and presence

Spec: `home-assistant-avgangsplan.md` §5.5, §10, §11.3, §12.

## Checklist

### Policy (§12.1)
- Timing: evening at 20:00, morning 60 min before, reminder 10 min before, leave now, change alert, critical change, cleanup on departure. TTS and family chat are v1.1 and must not be required by v1 code.
- Change alerts compare against the **last notified** time. If nothing has been sent yet, compare against the first published revision. Worsening alerts immediately. Improvement needs two stable updates and never moves the departure later unsafely. There is a cooldown of about 5 min, and critical changes bypass it.
- Quiet hours (21:00–06:00, per profile): a morning notice inside them moves to the end of quiet hours, or is skipped if less than 15 min would remain. Reminder, leave-now and critical alerts for today's active trip always go through. Evening and next-day notices are held. No critical-notification that overrides Do Not Disturb.
- `NotificationPolicy.evaluate` is pure, with `now` injected.

### Deduplication and restart (§11.3)
- Ledger key is `(mission_id, kind)`, where `mission_id = person:date:slot`. Flag any key built only from `(person, date)`, because that breaks future multi-trip support (§8).
- A plan that moves back does not resend the same step.
- After restart: recompute before sending. A missed leave-now is sent within 2 min only if the trip is still reachable; otherwise a late or disruption status is sent instead.
- The Android `tag` is stable per mission so duplicates replace each other. There are no persistent claims in v1, so flag over-engineering.

### Dispatcher (§12.2)
- The integration calls the script configured per profile and channel directly. There is no dispatch automation.
- Targets come only from the options-flow allowlist, never from intent fields.
- Each channel runs separately with a timeout, so one failure does not stop the others or the dashboard. Global dry-run logs instead of sending.
- The `family_departures_notification` event is emitted only as a hook and is not used for delivery.
- The payload contains no secrets. Privacy mode text exists.

### Companion and Live Update (§12.3)
- Android only. A title is always set. Live Update uses `live_update: true` and a stable tag, and is cleared with `clear_notification`. Plain status push is the fallback when Android is older than 16.
- There are no per-second pushes. Use the chronometer or local countdown.
- Action IDs carry person, mission and a nonce. A `mobile_app_notification_action` is validated against the active mission and the intended phone, so yesterday's "Jag går nu" does nothing today.
- The "Packat" action acknowledges the list for the notice's own date only.

### Presence (§10)
- Auto-departure only after 2 min stable away status and only inside the window from `recommended − 15 min` to the timeout. A dog walk at 06:50 must not close the trip.
- Unknown or stale presence is neither home nor departed. Normal reminders continue and manual confirmation works.
- Already away at morning start suppresses leave-home notices. Coming home never reopens the trip.
- Timers stop on departure, day off, or 60 min after `event_start`.

### Packing (§5.5)
- The evening notice includes tomorrow's list. A separate 20:00 packing notice is sent only if the evening notice is off. The list is repeated in the morning and 10-min notices unless acknowledged. There is no extra night push.

## Output

Per finding: `severity`, `file:line`, the problem, the spec §, and a fix. For timing bugs, give a concrete timeline (clock times plus events) that reproduces it. Explicitly list scenarios that could cause duplicate or missing notifications.
