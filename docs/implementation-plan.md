# Implementation plan – `family_departures`

Source of truth: `home-assistant-avgangsplan.md` (the spec, v1.1). This plan turns spec Steps 0–3 (the v1 boundary, §17) into dispatchable tasks. Steps 4 (v1.1) and §18 are out of scope except where noted.

Machine-readable checklist: `docs/implementation-plan.json` (same IDs, topologically ordered, for a ralph-style loop).

## 1. Rules for every agent task

1. Read the spec sections listed in the task and §1–§3 before coding. If the spec and this plan conflict, the spec wins; record the conflict in the task's PR/commit message.
2. Only create or modify files listed under the task's **Files** (plus tests and fixtures for them). Need to change a shared contract (§3 of this plan)? Stop and report instead of editing it.
3. Pure modules (`models.py`, `timeutil.py`, `schedule.py`, `packing.py`, `planner.py`, `notification_policy.py`, provider parsers) must not import `homeassistant` and must take `now` as a parameter.
4. All datetimes are timezone-aware UTC internally; local dates are computed in `Europe/Stockholm`.
5. Never write real ICS URLs, tokens, names of real schools, real addresses or coordinates into the repo. Local secrets live in `local/` (gitignored). Fixtures are sanitised.
6. Definition of done for every task: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy custom_components` and `uv run pytest -q` pass; the acceptance criteria below are covered by tests; the listed review skill has been applied and its blocker/major findings fixed.
7. One branch and one commit series per task, named `task/<ID>-<slug>`. Do not push or merge unless instructed.
8. Mark the task done in `docs/implementation-plan.json` (`"done": true`) as the last action.

## 2. Human tasks (cannot be done by agents)

These run in parallel with the agent work. Outputs go into `local/` (gitignored) unless stated. Agent tasks that depend on them say so; until then they use synthetic data.

| ID | Task | Output | Blocks |
| --- | --- | --- | --- |
| H1 | ✅ HA Core **2026.9.4** on the Green. Take a backup before first install | – | – |
| H2 | ◐ Companion installed on Parent A's and Parent B's phones. Kid A and Kid B follow once v1 works. Note `notify.mobile_app_*` names and Android versions for the two installed phones | `local/env.md` | T18 live test (Parent A's phone only) |
| H3 | Get Kid A's Vklass ICS URL; run `scripts/inspect_ics.py` (from T03) on it | Structure report (no content) committed as `docs/sources/vklass.md` | T03 Vklass filter defaults |
| H4 | Collect home and four destination coordinates; usual car route | `local/env.md` | T21 real-route acceptance |
| H5 | In HA: run `waze_travel_time.get_travel_times` with `realtime: true` and with `realtime: false` + `time_delta` for tomorrow 07:30; check whether a Waze config entry is required | Findings in `docs/decisions/waze.md` | T10 evening mode |
| H6 | Create Local Calendars for Parent A and Parent B with recurring events | – | T21 |
| H7 | Regenerate the SchoolSoft feed link if possible (old one was shared in chat) | New URL in config flow only | – |

### Environment facts (from H1/H2)

- HA Core 2026.9.4 requires Python **>= 3.14.2**. The local uv install has only 3.14.0rc1, so T01 must pin `requires-python = ">=3.14.2"` and let uv download a matching interpreter.
- Test harness: `pytest-homeassistant-custom-component==0.13.367`, which pins `homeassistant==2026.9.4`.
- During development **all real notifications go to Parent A's phone only** (test recipient mode, see T12/T18). Kid A's and Kid B's profiles run without phones until they are installed; presence is unset for them.

## 3. Shared contracts (implemented by T02, frozen afterwards)

Parallel tasks code against these. Names are binding; fields may be added by T02 only.

```python
# models.py  – frozen dataclasses, slots=True
Mode = Literal["public_transport", "car", "static"]
Attendance = Literal["normal", "off", "remote", "sick"]
Quality = Literal["realtime", "scheduled", "estimated", "stale", "unavailable"]
MissionStatus = Literal["no_event", "scheduled", "preparing", "leave_now", "late",
                        "departed", "skipped", "cannot_arrive_on_time", "needs_configuration"]
DayStatus = Literal["has_event", "no_activity", "day_off", "no_schedule", "source_error"]

ScheduleEvent(uid: str, summary: str, start: datetime, end: datetime, source_id: str)
ScheduleResult(status: Literal["ok", "empty", "error"], events: tuple[ScheduleEvent, ...],
               fetched_at: datetime, source_modified_at: datetime | None,
               content_hash: str | None, error_code: str | None, stale: bool)
SourceFilter(exclude_patterns: tuple[str, ...], include_patterns: tuple[str, ...])
PackingRule(id: str, match: str, item: str)
PackingList(person_id: str, local_date: date, items: tuple[str, ...], acknowledged: tuple[str, ...])
Margins(arrival: int, departure: int, boarding: int, parking_and_walk: int, min_transfer: int)  # minutes
ProfileConfig(id, name, source_type: Literal["ics", "ha_calendar"], calendar_entity_id | None,
              source_filter, destination_id, dest_lat, dest_lon, default_mode: Mode,
              static_minutes | None, static_label | None, weather_adjust: bool,
              car_fallback_minutes | None, margins: Margins, weekday_mask: frozenset[int],
              packing_rules: tuple[PackingRule, ...], person_entity_id | None,
              notifications_enabled: bool, change_threshold_minutes: int,
              quiet_start: time, quiet_end: time, scripts: dict[str, str],  # channel -> script entity
              evening_notice_enabled: bool = True)  # added in T15 by approval: off => packing-only evening notice (§5.5, §12.1)
              # ICS URL is NOT in ProfileConfig; it is read from entry.data by the coordinator only.
DayOverride(person_id, local_date: date, mode: Mode | None, attendance: Attendance | None,
            arrival_time: time | None)
ArrivalRequirement(mission_id, person_id, local_date: date, event_id, event_start, arrival_deadline,
                   destination_id, source)
RequirementOutcome(day_status: DayStatus, requirement: ArrivalRequirement | None,
                   reason_codes: tuple[str, ...])
Leg(kind: Literal["walk", "transit"], line: str | None, direction: str | None,
    from_stop: str | None, to_stop: str | None, platform: str | None,
    planned_departure, estimated_departure | None, planned_arrival, estimated_arrival | None,
    cancelled: bool, trip_ref: str | None)
Journey(journey_id: str, legs: tuple[Leg, ...])
JourneyResult(status: Literal["ok", "empty", "error"], journeys: tuple[Journey, ...],
              fetched_at: datetime, has_realtime: bool, error_code: str | None)
DurationResult(minutes: float | None, fetched_at: datetime,
               source: Literal["waze", "fallback", "static"], quality: Quality, route_name: str | None)
MarginBreakdown(travel: int, access_walk: int, boarding: int, departure: int, arrival: int, extra_after: int)
DeparturePlan(mission_id, plan_id, revision, requirement, mode, recommended_leave, latest_leave,
              last_on_time_alternative_leave, predicted_arrival, journey_id, route_summary,
              quality, feasible: bool, status: MissionStatus, breakdown: MarginBreakdown | None,
              reason_codes: tuple[str, ...], config_revision: int)
NotifiedRecord(kind: str, sent_at: datetime, leave_time: datetime | None, revision: int)
MissionState(mission_id, status: MissionStatus, departed_at | None, reopened: bool,
             notified: Mapping[str, NotifiedRecord], first_published_leave | None,
             action_nonce: str)
NotificationIntent(person_id, mission_id, plan_id, revision, notification_id, kind, severity,
                   title, message, recommended_leave_time, latest_leave_time, quality,
                   reason_codes, packing_items: tuple[str, ...], channels: tuple[str, ...],
                   tag: str, action_nonce: str)
```

```python
# timeutil.py
TZ = ZoneInfo("Europe/Stockholm")
def local_date_of(dt: datetime) -> date
def local_day_bounds(d: date) -> tuple[datetime, datetime]       # UTC start, end
def combine_local(d: date, t: time) -> datetime                  # -> UTC aware
def make_mission_id(person_id: str, d: date, slot: str = "morning") -> str

# schedule.py
def select_requirement(profile: ProfileConfig, d: date, result: ScheduleResult,
                       override: DayOverride | None, holiday: bool,
                       previous_events: tuple[ScheduleEvent, ...] | None,
                       state: MissionState | None, now: datetime) -> RequirementOutcome

# packing.py
def build_packing_list(events, rules, override, acknowledged) -> PackingList

# planner.py
def plan_fixed(req, mode, duration: DurationResult, profile, state, now, revision) -> DeparturePlan
def plan_transit(req, result: JourneyResult, profile, state, previous: DeparturePlan | None,
                 now, revision) -> DeparturePlan

# notification_policy.py
def evaluate(previous: DeparturePlan | None, current: DeparturePlan, state: MissionState,
             packing: PackingList, profile: ProfileConfig, now: datetime) -> list[NotificationIntent]

# providers/base.py  (Protocols)
class ScheduleProvider: async def async_get_day(self, d: date) -> ScheduleResult
class JourneyProvider:  async def async_plan(self, origin, destination, deadline: datetime,
                                             earliest_departure: datetime | None) -> JourneyResult
class CarProvider:      async def async_get_duration(self, origin, destination,
                                                     realtime: bool, time_delta: timedelta | None) -> DurationResult
```

## 4. Task waves

Tasks in the same wave can run in parallel once their dependencies are done.

| Wave | Tasks |
| --- | --- |
| 0 | T01, T00 |
| 1 | T02 |
| 2 | T03, T04, T05, T06, T07, T09, T10, T11, T15 |
| 3 | T08, T12 |
| 3b | T13 |
| 4 | T14, T16, T17 |
| 5 | T18, T19 |
| 6 | T20, T21 |

## 5. Tasks

### T00 – SL Journey Planner v2 investigation (agent, read-only + fixtures)
- **Depends on:** –
- **Spec:** §6.1, §6.2, §17 Steg 0
- **Files:** `docs/decisions/sl-realtime.md`, `tests/fixtures/sl/*.json`
- **Do:** Read the current Trafiklab Journey Planner v2 OpenAPI spec. Using public, non-private coordinates (e.g. two well-known central Stockholm stations), call the API for an arrival-based search. Capture 3–5 responses: normal, with realtime estimates if present, and any cancelled/deviation example you can find. Strip nothing private (there is none) but trim to what tests need.
- **Acceptance:**
  - Document exact request parameters for arrival-time search, date/time format and time zone handling.
  - Document per-leg fields: planned/estimated times, cancellation flag, platform, line, direction, stop IDs, trip identifiers, deviation info.
  - Clear decision: **A** Journey Planner v2 alone is sufficient for realtime (no `sl_realtime.py`), or **B** separate realtime adapter needed, with reasons.
  - Note rate limits/terms found.
- **Review skill:** review-journey-planning

### T01 – Repository scaffold and tooling
- **Depends on:** –
- **Spec:** §14, §4.1 (manifest)
- **Files:** `pyproject.toml`, `uv.lock`, `.gitignore`, `.pre-commit-config.yaml`, `custom_components/family_departures/{__init__.py,manifest.json,const.py,strings.json,translations/sv.json}`, `hacs.json`, `tests/conftest.py`, `tests/test_init.py`, `tests/test_no_secrets.py`, `README.md` (stub)
- **Do:**
  - `uv` project, `requires-python = ">=3.14.2"`, dev deps pinned exactly: `pytest-homeassistant-custom-component==0.13.367` (= HA 2026.9.4), `ruff`, `mypy`, `freezegun`. `manifest.json`/`hacs.json` minimum HA version 2026.9.0 until older versions are tested.
  - ICS library: use `ical` at the exact version shipped by that HA Core (HA's own Local Calendar uses it). Record the version in `manifest.json` `requirements`. If it cannot expand the needed RRULE cases, fall back to `icalendar` + `recurring-ical-events` and document why.
  - `.gitignore` includes `local/`, `.venv/`, caches.
  - Minimal `async_setup_entry`/`async_unload_entry` that load and unload cleanly.
  - `test_no_secrets.py`: fails if any tracked file matches `ical-feed/parent/[A-Za-z0-9_-]{20,}`, Vklass token URLs, or `latitude: 5[5-9]\.\d{4,}` style real coordinates outside an allowlist.
- **Acceptance:** `uv run pytest` passes; integration stub loads in the PHACC `hass` fixture and unloads with no lingering timers; manifest has no invented URLs (use placeholders the user must fill, flagged in README).
- **Review skill:** review-ha-integration

### T02 – Domain models and time utilities
- **Depends on:** T01
- **Spec:** §7, §8, §10
- **Files:** `models.py`, `timeutil.py`, `providers/base.py`, `tests/test_models.py`, `tests/test_timeutil.py`
- **Do:** Implement §3 contracts exactly. Add `__post_init__` validation: aware datetimes, non-negative margins, valid mode/attendance.
- **Acceptance:** naive datetime raises; `local_date_of` correct around DST (2026-03-29 and 2026-10-25) and around midnight UTC; `make_mission_id("kid_a", date(2026,10,1)) == "kid_a:2026-10-01:morning"`; mypy strict passes on these files.
- **Review skill:** review-ha-integration (purity), review-tests

### T03 – ICS parser and fetcher
- **Depends on:** T02
- **Spec:** §5.1, §5.2, §11.2, §15
- **Files:** `providers/ics.py`, `scripts/inspect_ics.py`, `tests/test_provider_ics.py`, `tests/fixtures/ics/{schoolsoft_sample.ics,rrule_exdate.ics,floating.ics}`
- **Do:**
  - `parse_ics(data: bytes, d: date, source_filter, source_id) -> tuple[ScheduleEvent, ...]` (pure; run in executor by caller). Honour TZID/VTIMEZONE, expand recurrences, drop all-day events, apply include/exclude patterns case-insensitively.
  - `IcsScheduleProvider(hass-free: session, url, filter)`: `async_fetch()` with 10 s timeout, 2 MB cap, redirects only to same host, ETag/Last-Modified if offered, SHA-256 content hash; unchanged hash → reuse previous parse. `async_get_day(d)` returns `ScheduleResult`; network/HTTP errors → `status="error"` with cached events and `stale=True`.
  - `disappeared_uids(previous, current)` helper for cancellation detection.
  - URL never appears in logs or exceptions (log host only).
  - `scripts/inspect_ics.py <url>`: prints structure only (property counts, TZIDs, summary prefixes with counts, first-of-day distribution, header caching info), no content, for H3.
  - SchoolSoft fixture: synthetic, mimicking verified structure (§5.2): `Lektion <CODE>` summaries incl. `LUNCH`, `MENTOR`, `STÖD`, `IDRO1000X`; `TZID=Europe/Berlin` with VTIMEZONE; `X-PUBLISHED-TTL:PT1H`; no STATUS/CATEGORIES; a few weeks spanning the October DST change.
- **Acceptance:** tests for: Berlin TZID → correct Stockholm local times in summer and winter; RRULE+EXDATE+RECURRENCE-ID; LUNCH excluded; unchanged hash skips reparse; HTTP 500 → error with stale cache; oversized response rejected; log output contains no URL path.
- **Review skill:** review-schedule-sources, review-privacy-security

### T04 – HA calendar provider
- **Depends on:** T02
- **Spec:** §4.3, §5.3 (`calendar.get_events`)
- **Files:** `providers/ha_calendar.py`, `tests/test_provider_ha_calendar.py`
- **Do:** `HaCalendarScheduleProvider(hass, entity_id, filter)` calling `calendar.get_events` with `return_response=True` for the local day bounds. Map to `ScheduleEvent` (UTC). Unavailable entity or service error → `status="error"`. Also used for the household holiday calendar (all-day "Ledig" detection helper `async_is_holiday(d)`).
- **Acceptance:** PHACC test with a real `local_calendar` entry: recurring event, edited single occurrence, deleted occurrence; missing entity → error, not empty.
- **Review skill:** review-schedule-sources, review-ha-integration

### T05 – Schedule selection
- **Depends on:** T02
- **Spec:** §4.3, §5.3, §5.4
- **Files:** `schedule.py`, `tests/test_schedule.py`
- **Do:** Implement `select_requirement` per contract: weekday mask, holiday, override (`off/sick/remote` → `day_off`; `arrival_time` replaces start), first event of whole local day, lock after departure (`state.status == "departed"` and not reopened → keep previous requirement), before departure a disappeared/cancelled first event moves to next, `arrival_deadline = start − margins.arrival`.
- **Acceptance:** every row of the "Situation/Beteende" table in §5.4 and the §16 rows: two lessons + early task; first lesson passed; first cancelled before departure; change after departure; empty Saturday vs Tuesday; HTTP error vs empty; Parent A 09:00 with arrival margin 0.
- **Review skill:** review-schedule-sources

### T06 – Packing list
- **Depends on:** T02
- **Spec:** §5.5
- **Files:** `packing.py`, `tests/test_packing.py`
- **Acceptance:** PE as third lesson → item; two PE lessons → one item; override `sick` → empty; acknowledged items excluded, new item still shown; removed lesson removes item.
- **Review skill:** review-schedule-sources

### T07 – Planner: car and static
- **Depends on:** T02
- **Spec:** §6.3 (fallback), §6.4, §7
- **Files:** `planner.py` (functions `plan_fixed` and shared helpers), `tests/test_planner_fixed.py`
- **Do:** Formulas from §7; weather surcharge only if `weather_adjust` (max of active surcharges); `DurationResult.minutes is None` and no fallback → `status="needs_configuration"`, no times; status derivation from `now` (scheduled / preparing / leave_now / late); populate `MarginBreakdown`.
- **Acceptance:** worked example tests with exact minutes; fallback marked `estimated`; never zero travel time from missing data; parking/walk added for car only.
- **Review skill:** review-journey-planning

### T08 – Planner: public transport
- **Depends on:** T02, T00, T07 (shares `planner.py`)
- **Spec:** §6.1, §6.2, §7
- **Files:** `planner.py` (function `plan_transit`), `tests/test_planner_transit.py`
- **Do:** Candidate filtering (all transfers ≥ `min_transfer`, final arrival ≤ deadline, first boarding reachable from `now` + walk + boarding), selection of latest safe home departure, tie-break on fewer transfers, stickiness to `previous.journey_id` unless improvement > 2 min, `last_on_time_alternative_leave`, cancelled leg → invalid, no feasible → `cannot_arrive_on_time` with best late, delayed first bus does not move departure later.
- **Acceptance:** §16 rows: SL bus cancelled; second leg delayed / missed transfer; delayed bus may recover; no journey in time; plus the §7 worked example (08:20 → 07:37 / 07:32).
- **Review skill:** review-journey-planning

### T09 – SL Journey Planner v2 adapter
- **Depends on:** T02, T00
- **Spec:** §6.1, §11.2
- **Files:** `providers/sl_journey.py`, `tests/test_provider_sl.py` (+ `providers/sl_realtime.py` only if T00 decision is B)
- **Do:** Build the request exactly as documented in `docs/decisions/sl-realtime.md`; map to `Journey`/`Leg` with namespaced stop IDs; 10 s timeout; 429 with Retry-After; at most 3 candidates plus earlier window when needed; `has_realtime` reflects actual estimated fields.
- **Acceptance:** contract tests against T00 fixtures; request-builder test locks parameter names and local-time formatting; timeout/429/5xx → `status="error"`; opt-in live smoke test behind `FD_LIVE_SL=1`.
- **Review skill:** review-journey-planning

### T10 – Waze provider
- **Depends on:** T02 (H5 for evening mode)
- **Spec:** §6.3
- **Files:** `providers/waze.py`, `tests/test_provider_waze.py`
- **Do:** Call `waze_travel_time.get_travel_times` via `hass.services.async_call(..., blocking=True, return_response=True)` with coordinates, `region: "eu"`, `realtime`, optional `time_delta`. Pick shortest valid route. Cache per (origin, destination) for 10 min; dedupe concurrent calls. Errors, empty list, non-numeric → `minutes=None`.
- **Acceptance:** tests with a mocked service returning: normal, empty, string duration, raising `ServiceNotFound`; cache hit within TTL; single call for two profiles sharing a route.
- **Review skill:** review-journey-planning

### T11 – Store and mission state
- **Depends on:** T02
- **Spec:** §5.5, §8 (mission key), §11.3, §15
- **Files:** `store.py`, `tests/test_store.py`
- **Do:** Versioned `homeassistant.helpers.storage.Store` holding: overrides per (person, date), `MissionState` per mission_id, packing acknowledgements per (person, date), last schedule hash + events (today/tomorrow only), last plans. Debounced save. Prune missions/acks older than 7 days on load. Migration hook from version 1.
- **Acceptance:** round-trip of all records; prune; migration test with a hand-written v1 payload; corrupted file → empty state + logged warning, not crash.
- **Review skill:** review-ha-integration

### T15 – Notification policy
- **Depends on:** T02
- **Spec:** §5.5 (delivery), §12.1, §12.2
- **Files:** `notification_policy.py`, `tests/test_notification_policy.py`
- **Do:** Pure `evaluate()` producing intents for evening, packing-only evening, morning (−60), reminder (−10), leave_now, change, critical, cleanup; baseline = last notified leave or `first_published_leave`; worsening immediate, improvement needs two consecutive revisions; 5 min cooldown bypassed by critical; quiet hours rule (shift morning to quiet end, skip if < 15 min left; today's reminder/leave_now/critical always pass); dedup by `(mission_id, kind)` from `state.notified`; packing items unless acknowledged; Swedish message texts; stable `tag = f"departure_{mission_id}"`.
- **Acceptance:** §16 rows: small changes summing to 4 min; plan moves back (no resend); morning notice at 05:45 → 06:00; packing in evening and morning, absent after ack; no intents after `departed`.
- **Review skill:** review-notifications

### T12 – Config flow and options flow
- **Depends on:** T02, T03, T04
- **Spec:** §2, §4.1, §4.2, §4.3, §5.5
- **Files:** `config_flow.py`, `strings.json`, `translations/sv.json`, `tests/test_config_flow.py`
- **Do:** User step: household + home coordinates (location selector). Options flow menu: add/edit profile (user-defined profiles; the id is the slug of the profile name, e.g. kid_a/kid_b/parent_a/parent_b), source (ICS URL as password-type field validated by a test fetch; or calendar entity), filters, destination, travel mode + all mode settings (SL settings shown regardless of default mode), margins with defaults (kids 5, adults 0 arrival), weekday mask, packing rules with preview of next matching dates, quiet hours, scripts per channel (entity selector, domain `script`), notifications enabled, **evening notice enabled** (`evening_notice_enabled`, default on; when off the profile gets a packing-only notice at 20:00 instead of the full evening summary — spec §5.5, §12.1), global dry-run, and global **test recipient** (one script entity, optional). Script per channel is optional per profile so Kid A and Kid B can be configured before they have phones. Final step shows a test calculation (Parent B: car and transit). ICS URLs stored in `entry.data`, everything else in `entry.options`.
- **Acceptance:** flow tests for happy path, invalid ICS URL (HTTP error, non-calendar), missing script entity, options change triggers reload; snapshot of `entry.data` shows URL only there; strings exist in sv.
- **Review skill:** review-ha-integration, review-privacy-security

### T13 – Coordinator
- **Depends on:** T03, T04, T05, T06, T07, T08, T09, T10, T11
- **Spec:** §3, §11.1, §11.2
- **Files:** `coordinator.py`, `tests/test_coordinator.py`
- **Do:** One coordinator per entry. Per profile: fetch schedule (executor parse), select requirement, build packing list, get duration/journeys per mode (today's override mode wins), plan, persist, publish. Update cadence from §11.1 derived from the preliminary plan (adaptive `update_interval` or explicit scheduled refreshes); SchoolSoft hourly / 15 min in morning window; tomorrow preview computed separately. Per-profile lock, max 2 concurrent external calls, single-flight per cache key, `config_revision` guard so stale results are dropped. One profile failing doesn't fail others.
- **Acceptance:** tests with fake providers: mode switch mid-fetch → stale result discarded; Kid B source error leaves Kid A fine; static plan triggers no network; tomorrow preview does not replace today's plan; call counts in morning window match §11.1.
- **Review skill:** review-ha-integration, review-journey-planning

### T14 – Entities
- **Depends on:** T13
- **Spec:** §9, §5.5
- **Files:** `entity.py`, `sensor.py`, `binary_sensor.py`, `select.py`, `switch.py`, `button.py`, `tests/test_entities.py`
- **Do:** Entity set from §9 per profile, one device per profile, unique IDs `{entry_id}_{profile_id}_{key}`, translation keys. `select.*_today_transport` and `*_today_attendance` write a `DayOverride` for today via store and request refresh. Attributes limited to §9 list plus `breakdown`. Status sensor uses device class `enum`.
- **Acceptance:** registry stability across profile rename; timestamp sensors `None` when no plan; changing today's transport recalculates and old plan's timers cannot restore it; attributes contain no URL/coordinates.
- **Review skill:** review-ha-integration, review-privacy-security

### T16 – Services
- **Depends on:** T13
- **Spec:** §14 actions table
- **Files:** `services.py`, `services.yaml`, `tests/test_services.py`
- **Do:** Register the seven actions with voluptuous schemas; `get_alternatives` returns response data; validation of person, date, mission/revision; `set_override` with future dates.
- **Acceptance:** each service happy path + invalid person/date/revision; `arrival_time` combined in local tz on a DST date; services removed on last entry unload.
- **Review skill:** review-ha-integration

### T17 – Scheduler
- **Depends on:** T13, T15
- **Spec:** §10 (timeouts), §11.3
- **Files:** `scheduler.py`, `tests/test_scheduler.py`
- **Do:** For each published plan, compute next trigger times (evening 20:00, −60, −10, recommended, timeout event_start+60) and register `async_track_point_in_utc_time`; cancel and re-register on new revision; callback re-checks mission/revision/state, runs `evaluate()`, records `NotifiedRecord` in store before dispatch returns; 60 s local tick for missed thresholds (no network); restart catch-up (leave_now within 2 min only if still reachable, otherwise late/disruption status).
- **Acceptance:** §16 rows: restart around 10-min warning (no duplicate, sensible catch-up); unload/reload leaves no timers; timeout stops everything 60 min after start even with broken presence.
- **Review skill:** review-notifications, review-ha-integration

### T18 – Dispatcher, notification actions and example scripts
- **Depends on:** T17
- **Spec:** §12.2, §12.3, §12.4 (privacy mode)
- **Files:** `dispatcher.py`, `examples/scripts.yaml`, `tests/test_dispatcher.py`
- **Do:** For each intent and channel, call `script.turn_on` on the configured script with intent variables; per-channel timeout and isolation; dry-run logs + fires event only; always fire `family_departures_notification`.
  - **Test recipient mode:** when the global test recipient is set, every intent from every profile goes only to that script (Parent A's phone), never to the profile's own scripts. Title is prefixed `[TEST <Name>]` and `tag` gets the person ID so the four profiles don't replace each other's notifications. Action buttons still carry the real person's mission ID. The dashboard and README show clearly that test mode is on.
  - A profile without a configured script is skipped silently (logged at debug), not an error. Listen to `mobile_app_notification_action`; parse `FD_DEPARTED|<mission_id>|<nonce>`, `FD_PACKED|...`, `FD_OFF|...`; validate mission is current and nonce matches, then update store. Example push script for Android: title, message, `tag`, `live_update: true` when Android ≥ 16 flag, actionable buttons, `clear_notification` on cleanup, privacy-mode variant.
- **Acceptance:** test recipient set → Parent B's and Kid A's intents both reach only the test script, with prefixed titles and distinct tags, and Parent B's own script is never called; one channel raising → other channels and dashboard unaffected; yesterday's action is a no-op; `FD_PACKED` from evening notice acknowledges tomorrow; dry-run never calls scripts; script YAML validates (`script` config schema check in test).
- **Review skill:** review-notifications, review-privacy-security

### T19 – Diagnostics and repairs
- **Depends on:** T13
- **Spec:** §15
- **Files:** `diagnostics.py`, `repairs.py` (or issue registry calls in coordinator), `tests/test_diagnostics.py`
- **Do:** Config-entry diagnostics with `async_redact_data` (URLs, coordinates, names, event summaries/descriptions); per-source health (last success, last error code, cache hits, external call counts). Repairs issues: invalid source, missing destination, repeated fetch failures (≥ 3 consecutive).
- **Acceptance:** diagnostics snapshot contains no URL, token, coordinate or summary text; repair issue created and cleared.
- **Review skill:** review-privacy-security

### T20 – Dashboard example, README, translations
- **Depends on:** T14, T18
- **Spec:** §13, §15 (backup/rollback), §17 Steg 5 docs
- **Files:** `examples/dashboard.yaml`, `README.md`, `translations/sv.json` (final pass), `strings.json`
- **Do:** Family overview sorted by next recommended departure, per-person cards, tomorrow view, packing list display, distinct texts for "Inget schema registrerat" / "Ledig" / "Schemakälla kunde inte hämtas". README: HACS install, minimum HA version, setup walkthrough, Companion steps, dry-run and shadow-mode procedure, privacy notes (link tokens, non-admin accounts), backup/rollback/uninstall, known limitations.
- **Acceptance:** dashboard YAML loads in a PHACC test or via schema check; README has no real data; every entity in the dashboard exists in §9.
- **Review skill:** review-privacy-security

### T21 – End-to-end scenario tests and coverage audit
- **Depends on:** T14, T16, T17, T18, T19
- **Spec:** §16, §17 "Klart när" for Steps 1–3
- **Files:** `tests/test_e2e_*.py`, `docs/test-coverage.md`
- **Do:** Full-integration tests with frozen time and fake SL/Waze/ICS: a whole morning for each profile (evening → morning → reminder → leave_now → departed → cleanup), plus a cancelled-bus morning and a restart morning. Produce the §16 coverage table.
- **Acceptance:** every §16 row maps to a passing test or is explicitly marked out of v1 scope (separate realtime matching if T00 = A; TTS/chat). Step 1–3 done criteria demonstrated.
- **Review skill:** review-tests

## 6. After agent work (human, spec §17 Steg 5)

1. Install on the Green via HACS custom repository; configure with real sources (H2–H7).
2. Dry-run five weekdays; compare plans with real departures; tune margins.
3. With test recipient = Parent A's phone, run real (not dry-run) notifications for all four profiles. When that works, clear the test recipient, install Companion on Kid A's and Kid B's phones (rest of H2), and enable push one person at a time after a test notification; run one week live.
4. Then plan v1.1 (TTS, family chat, presence auto-departure, kitchen dashboard).

## 7. Dispatch notes

- Waves 2 and 4 are the parallel fan-out points; give each agent this file, the spec and its task ID.
- Contracts in §3 are the merge risk. Merge T02 before dispatching wave 2, and treat any contract change as a coordinated change across open tasks.
- `planner.py` is shared by T07 and T08: T07 owns helpers and `plan_fixed`, T08 adds only `plan_transit` and its private helpers, so T08 runs after T07.
- `strings.json`/`sv.json` are touched by T01, T12 and T20: T12 adds keys, T20 does the final language pass.
