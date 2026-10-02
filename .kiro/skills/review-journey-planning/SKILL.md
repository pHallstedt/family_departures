---
name: review-journey-planning
description: Review travel-time providers and the departure planner - SL Journey Planner v2 adapter, optional realtime adapter, Waze get_travel_times usage, static mode, margins, leave-time formulas, journey selection, cancellations and feasibility. Use when reviewing planner.py, providers/sl_journey.py, providers/sl_realtime.py, providers/waze.py or models.py.
---

# Review: journey providers and planner

Spec: `home-assistant-avgangsplan.md` §6, §7, §8, §11.2.

## Checklist

### Planner purity
- `planner.py` is plain Python. It imports nothing from `homeassistant` and takes `now` as a parameter, never reading the clock itself (§3, §8).
- Models are frozen dataclasses with aware datetimes. Mode is one of `public_transport`, `car`, `static`.

### Formulas (§7), so verify the arithmetic
- `arrival_deadline = event_start − arrival_buffer`.
- Car and static: `latest_leave = arrival_deadline − travel_duration − extra_after_travel`, and `recommended = latest − departure_buffer`. Parking and walking are added for car, never inside the Waze duration.
- SL: `first_vehicle_departure = min(planned, estimated)` when an estimate exists, then `latest = that − access_walk − boarding_buffer`, then `recommended = latest − departure_buffer`.
- Walking legs inside the journey are not subtracted twice. The final walk is part of arrival.
- `latest_leave` refers to the selected journey only. `last_on_time_alternative_leave_time` is a separate value, and the two are never mixed (§7).
- The margin breakdown is exposed in attributes so shadow mode can tune it.

### SL adapter (§6.1)
- Arrival-based search uses parameters verified against the current Journey Planner v2 OpenAPI spec and locked by a contract test. Nothing is copied from SL API 3.1 or ResRobot.
- Every leg is normalised with namespaced stop IDs, planned and estimated times, and cancellation status.
- The Step 0 realtime decision is respected. If Journey Planner v2 suffices, there must be no separate realtime adapter or trip matching. If a separate one exists, the matching rules in §6.2 apply and `ambiguous` matches are never applied.

### Disruption logic (§6.2)
- All legs and transfers are re-checked against the minimum transfer margin.
- Cancelled trip or missed transfer: replan from **now** plus walk and boarding margin. Never suggest an unreachable earlier bus.
- If no on-time option exists, use `cannot_arrive_on_time` with the best late arrival. Never switch travel mode automatically.
- A delayed bus does not push the home departure later by default. Earlier departures and cancellations take effect immediately.
- Missing realtime means unknown, never zero delay.
- The selected journey is kept when an alternative is only slightly better, to avoid jumpy advice. Fewer transfers win near-ties.

### Waze (§6.3)
- Uses the `waze_travel_time.get_travel_times` action with `return_response`, `region: eu` and `realtime: true` in the morning window. There are no per-route sensors.
- Durations are validated as numeric. An error, an empty list or a non-numeric value is never treated as 0.
- Fallback order: fresh value within TTL (10 min), then configured fallback time marked `estimated`, then a configuration error.
- The evening plan uses `realtime: false` with `time_delta` only if Step 0 verified it. It is always labelled preliminary.
- Calls are deduplicated per route per update round.

### Static mode (§6.4)
- No network calls. A weather surcharge applies only when `weather_adjust` is set; by default the highest active surcharge applies, not a sum. Missing weather data is reported as missing.

### Quality
- `quality` (realtime/scheduled/estimated/stale/unavailable) is tracked separately from status. A confirmed cancellation is never reset to normal just because updates stopped.

## Output

Per finding: `severity`, `file:line`, the problem, the spec §, and a fix. For formula bugs, include a worked example with concrete times showing the wrong and the right result.
