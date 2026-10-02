# SL Journey Planner v2 fixtures

Captured for task T00 (see `docs/decisions/sl-realtime.md`). All data is from
trips between **public central Stockholm stations** supplied as WGS84
coordinates, so there is no private data. Responses were trimmed to the fields
the adapter and contract tests need (bulky `coords` / `pathDescriptions` point
arrays removed; coordinate endpoints relabelled to a neutral placeholder).

| File | Source | Notes |
| --- | --- | --- |
| `arrival_normal.json` | Real, `itd_trip_date_time_dep_arr=arr`, future weekday 08:20 | 3 journeys, no delays, estimated == planned. |
| `realtime_monitored.json` | Real, near-"now" departure search | Transit leg carries `realtimeStatus: ["MONITORED"]` and `departureTimeEstimated` ~90 s later than planned — proves per-leg realtime estimates. |
| `cancelled_deviation.json` | **Synthetic**, derived from a real journey shape | One transit leg set to `realtimeStatus: ["TRIP_CANCELLED"]` with a deviation `infos` block. The exact cancellation flag and `infos` schema must be re-verified against a live disruption before the adapter relies on them (tracked in the decision doc). |
| `error_invalid_date.json` | Real | `systemMessages` error response (no `journeys` key) returned with HTTP 200 for a date outside the system validity window. |

Times in responses are **UTC** (ISO 8601 with a trailing `Z`). The request
`itd_date` / `itd_time` are interpreted in **local Europe/Stockholm** time.
