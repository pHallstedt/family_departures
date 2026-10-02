# Decision: SL Journey Planner v2 realtime source (spec §6.1, §6.2, §17 Steg 0)

Status: **decided** — Decision **A** (Journey Planner v2 alone covers v1 realtime).
Date: 2026-10-01. Scope: task T00. Downstream: T08, T09.

This records the Step 0 investigation of the Trafiklab **SL Journey Planner v2**
API: the exact arrival-search request contract, the per-leg fields the adapter
must map, and the realtime decision point from §6.1. Sources:
[SL Journey Planner v2](https://www.trafiklab.se/api/our-apis/sl/journey-planner-2/)
[S2] and the open-source
[NecroKote/trafiklab-sl](https://github.com/NecroKote/trafiklab-sl) client, which
mirrors the current API. Request parameters and field names below were verified
against **live responses** captured from the production endpoint using public
central-Stockholm station coordinates. Content was rephrased for compliance with
licensing restrictions.

## Endpoint

- Base URL: `https://journeyplanner.integration.sl.se/v2`
- Trip search: `GET /v2/trips`
- Stop search: `GET /v2/stop-finder`
- Validity window: `GET /v2/system-info` returns `{"validity": {"from", "to"}}`.
  Observed window at capture time was roughly 2026-06-30 … 2026-12-13, i.e. a
  few months ahead. **A date outside this window returns an error, not an empty
  result** (see error handling), so the adapter must only ever request today or
  tomorrow.
- **No API key** was required for any call during testing; requests carried no
  `key` parameter and succeeded. This matches the spec note in §6.1. If Trafiklab
  later gates the endpoint, add a key as a password-type field — do not log it.

## Request parameters for arrival-time search

All parameters are query-string parameters on `GET /v2/trips`.

| Parameter | Value | Notes |
| --- | --- | --- |
| `type_origin` | `coord` | Use coordinates for door-to-door. `any` for a stop id. |
| `name_origin` | `<lon>:<lat>:WGS84[dd.ddddd]` | **Longitude first**, then latitude. |
| `type_destination` | `coord` | |
| `name_destination` | `<lon>:<lat>:WGS84[dd.ddddd]` | |
| `calc_number_of_trips` | `1`–`3` | At most 3 alternatives (spec §6.1). |
| `itd_date` | `YYYYMMDD` | Date of the search, **local Europe/Stockholm**. |
| `itd_time` | `HHMM` | Time of the search, **local Europe/Stockholm**. |
| `itd_trip_date_time_dep_arr` | `arr` | **Selects arrival-time search.** Omit (or `dep`) for departure search. |
| `language` | `sv` | Swedish summary texts. |

Optional, likely useful later: `max_changes`, `route_type`
(`leasttime`/`leastinterchange`/`leastwalking`), `use_prox_foot_search`
(walk to nearby stops), and the `incl_mot_*` mode filters. Do **not** copy any
parameter from SL API 3.1 or ResRobot (spec §6.1).

### Date/time format and time zone — the critical finding

- **Request** `itd_date`/`itd_time` are interpreted in **local Europe/Stockholm**
  wall-clock time. A near-"now" search sent as local `1034` returned departures
  around 10:34 local.
- **Response** timestamps are **UTC**, ISO 8601 with a trailing `Z`
  (e.g. `2026-10-05T06:02:54Z`). This directly contradicts the comment in the
  `trafiklab-sl` client that labels response times as local; the live data is UTC.
  The adapter must parse response times as UTC (already our internal
  representation) and must build request times by converting the UTC `deadline`
  to Europe/Stockholm first.
- Contract test (T09) must lock: `itd_trip_date_time_dep_arr=arr`, the
  `YYYYMMDD`/`HHMM` formats, the local-time conversion of the deadline, and the
  `<lon>:<lat>:WGS84[dd.ddddd]` coordinate order.

## Response shape

Top level: `{"journeys": [...], "systemMessages": [...]}`. On error the
`journeys` key is **absent** and only `systemMessages` is present.

Each journey: `tripDuration` (s, planned), `tripRtDuration` (s, with realtime),
`interchanges`, `isAdditional`, `rating`, `legs`.

### Per-leg fields (mapping to `Leg` in §3)

| Need (§3 `Leg`) | JP v2 location | Observed |
| --- | --- | --- |
| kind walk/transit | `transportation.product.class` | `99`/`100` = footpath; else transit. |
| line | `transportation.disassembledName` (short, e.g. `17`), `transportation.number`, `transportation.name` | Transit legs only. |
| direction | `transportation.destination.name` (+ `.id`) | Headsign/direction. |
| from_stop / to_stop | `origin.id` + `origin.name`, `destination.id` + `destination.name` | Stop ids are **namespaced** (e.g. `9025001000002051`, or `<siteId>|<area>|<platform>`). |
| platform | `origin.properties.platform` / `platformName` | Present on platform-type stops. |
| planned_departure | `origin.departureTimePlanned` | UTC `Z`. |
| estimated_departure | `origin.departureTimeEstimated` | UTC `Z`; equals planned when no live data. |
| planned_arrival | `destination.arrivalTimePlanned` | UTC `Z`. |
| estimated_arrival | `destination.arrivalTimeEstimated` | UTC `Z`. |
| cancelled | `realtimeStatus` contains a cancellation flag | See realtime below. |
| trip_ref | `transportation.id` (e.g. `tfs:02017: :H:y01`) and `properties.tripId` | Both present on transit legs. |
| deviation info | `infos` (array, per leg) | Empty when no disruption. |
| realtime controlled | `isRealtimeControlled` (bool), `realtimeStatus` (list) | `["MONITORED"]` on live-tracked legs. |

Also present: `origin.departureTimeBaseTimetable` /
`destination.arrivalTimeBaseTimetable` (the base timetable time, separate from
planned). The walk legs at the start/end are part of the door-to-door journey and
their time must **not** be subtracted again when computing margins (spec §6.1, §7).

### Realtime, cancellation and deviation (decision point, §6.1)

Verified from live data:

- **Estimated times are returned per leg.** A near-now capture
  (`tests/fixtures/sl/realtime_monitored.json`) shows a metro leg with
  `realtimeStatus: ["MONITORED"]`, `isRealtimeControlled: true`, and
  `departureTimeEstimated` about 90 s later than `departureTimePlanned`. So live
  delay is expressed as `estimated` vs `planned` on the same search — exactly
  what re-querying the arrival search every 60 s in the last 30 minutes needs
  (spec §6.1).
- **Deviation channel exists per leg**: the `infos` array. It was empty in all
  captured no-disruption responses.
- **Cancellation**: expressed via `realtimeStatus` (EFA uses a
  `TRIP_CANCELLED`-style token) together with an `infos` entry. No live
  cancellation occurred during capture, so `tests/fixtures/sl/cancelled_deviation.json`
  is **synthetic**, built from a real journey shape. The exact cancellation token
  string and the full `infos` schema (`priority`, `infoLinks[].content/subtitle`,
  `properties`) must be re-verified against a real disruption before the planner
  treats a leg as cancelled — tracked as remaining adapter work (see below).

## Decision

**A — Journey Planner v2 alone is sufficient for v1 realtime.** Re-querying the
same arrival search (per spec: every 60 s during the final 30 minutes) yields the
estimated times, `isRealtimeControlled`/`realtimeStatus` and `infos` the planner
needs for the §6.2 behaviour (cancellation, missed transfer, delayed-bus rules).

Consequences, per §6.1:

- **No `sl_realtime.py`**, no SL Transport realtime adapter, and **no separate
  trip-matching** (§6.2 "Matchning") in v1. The matching rules only apply when
  realtime comes from a separate source; here it comes from the same search, so
  there is no `ambiguous` matching problem.
- The §6.2 rules that are source-independent **still apply** in `plan_transit`
  (T08): re-check every transfer against `min_transfer`; a cancelled leg or
  missed transfer invalidates the journey and replans from now; a delayed bus
  does not push the home departure later by default; missing realtime means
  unknown delay, never zero.
- `JourneyResult.has_realtime` reflects whether any selected leg had
  `isRealtimeControlled: true` / an `estimated` time, not merely that a response
  came back.

## Remaining adapter work (kept visible, per §17 "Obekräftade API-funktioner")

1. Re-verify the cancellation token in `realtimeStatus` and the `infos` schema
   against a **real** live disruption; update `cancelled_deviation.json` and the
   `Leg.cancelled` mapping in T09 accordingly. Until verified, treat the synthetic
   fixture's `TRIP_CANCELLED` as provisional.
2. Confirm Trafiklab's current **rate limits and terms** for JP v2. No key was
   required and no limit was hit in light testing, but published per-key limits
   were not confirmed in this task; T09 must still implement a 10 s timeout and
   handle `429` with `Retry-After` defensively.
3. Confirm how far ahead `system-info` validity extends in production so the
   evening (tomorrow) search never falls outside it.

## Fixtures

See `tests/fixtures/sl/README.md`. Captured between two public central-Stockholm
stations (no private data), trimmed to the fields above:
`arrival_normal.json`, `realtime_monitored.json`,
`cancelled_deviation.json` (synthetic), `error_invalid_date.json`.

## References

- [S2] Trafiklab, [SL Journey Planner v2](https://www.trafiklab.se/api/our-apis/sl/journey-planner-2/) (incl. current OpenAPI spec).
- [S10] Trafiklab, [SL Transport](https://www.trafiklab.se/api/our-apis/sl/transport/) — reserve only, not used in v1.
- [S11] Trafiklab, [SL Deviations](https://www.trafiklab.se/api/our-apis/sl/deviations/) — reserve only, not used in v1.
- [S12] Trafiklab, [Realtime APIs](https://www.trafiklab.se/api/our-apis/trafiklab-realtime-apis/) — reserve only, not used in v1.
- [NecroKote/trafiklab-sl](https://github.com/NecroKote/trafiklab-sl) — open-source client used to cross-check parameter names.
