---
name: review-schedule-sources
description: Review schedule and calendar handling - ICS adapter (Vklass, SchoolSoft), HA calendar adapter (Local Calendar), time zones, first-lesson selection, day locking, empty-vs-error results, weekday mask and packing-list rules. Use when reviewing providers/ics.py, providers/ha_calendar.py, schedule selection logic or packing rules.
---

# Review: schedule sources and first-event selection

Spec: `home-assistant-avgangsplan.md` §4.3, §5.1–5.5, §7, §11.2.

## Checklist

### Parsing
- ICS is parsed with a maintained library, including recurrence expansion. There is no home-made RRULE parser (§5.1).
- UID, DTSTART/DTEND, TZID/VTIMEZONE, RRULE/RDATE/EXDATE, RECURRENCE-ID and `STATUS:CANCELLED` are handled.
- SchoolSoft uses `TZID=Europe/Berlin` with its own VTIMEZONE. The code must honour the TZID and convert to `Europe/Stockholm`; it must not assume the times are local (§5.2). Floating times use the configured source time zone and are flagged.
- Parsing of the ~190 kB SchoolSoft feed runs in the executor.

### Fetching
- Vklass uses ETag/Last-Modified if the server supports them.
- SchoolSoft has neither, so it compares a content hash and skips reparse and recompute when unchanged. The interval honours `X-PUBLISHED-TTL:PT1H`: about hourly, and every 15 min in the morning window.
- Redirects and response size are bounded, and only the configured host is fetched (§15).

### Filtering
- All-day events, tasks and deadlines are excluded. Filters are configurable per source (include/exclude patterns).
- SchoolSoft excludes `LUNCH` by default. `MENTOR`/`STÖD` stay included and visible in diagnostics.
- The code never assumes the earliest calendar entry is a lesson unless a verified filter says so.

### Result semantics (most common bug area)
- Three distinct outcomes: fetch error, successful empty schedule, explicit day off. They are never collapsed into one (§5.3).
- Empty schedule on a day outside the weekday mask is silent. On an expected day it shows "Inget schema registrerat" with no morning reminders. A fetch error falls back to stale cache, marked as such (§5.4).
- An empty or failed result is never treated as a confirmed day off.

### First event and locking (§5.3)
- The first event is chosen from the whole local day, not only future events. A passed 08:20 does not make 09:15 a new morning trip.
- Before departure, a cancelled first lesson moves the start to the next valid lesson. For SchoolSoft, cancellation is detected as a UID that disappeared since the previous fetch.
- After departure the morning trip is closed. Only explicit `reopen_today` reopens it; calendar updates and returning home do not.
- Day overrides `off`/`sick`/`remote` close the trip and all timers.
- For a flextime adult (e.g. Parent A) the calendar start is the latest arrival time. The arrival margin is `is_adult`-driven: adult profiles default `arrival_minutes` to 0 (the calendar time is already the arrival requirement), children to 5 (time to reach the classroom) (§4.3).

### Day keys and time
- Day keys are computed in `Europe/Stockholm`, never from server UTC. DST transitions are covered by tests.
- Events are stored as aware UTC datetimes.

### Packing rules (§5.5)
- Rules match case-insensitively against all included lessons of the day, not just the first.
- Items are deduplicated. Overrides empty the list. A removed lesson removes its line.
- `build_packing_list` is pure and has no effect on departure times.
- Acknowledgement is stored per `(person_id, local_date)` and covers only the items present at the time; new items show again.

## Output

For each finding give `severity`, `file:line`, the problem, the spec § and a fix. Point out every place where the three result types could be confused, even if it is not certain to be a bug. Never paste real schedule content, ICS URLs or tokens into the review.
