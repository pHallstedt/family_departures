---
name: review-ha-integration
description: Review Home Assistant integration code in custom_components/family_departures for HA Core conventions - async I/O, DataUpdateCoordinator, config/options flow, entities, Store, services, unload/reload and manifest. Use when reviewing changes to __init__.py, coordinator.py, config_flow.py, entity platforms, store.py, services.yaml or manifest.json.
---

# Review: Home Assistant integration mechanics

Spec: `home-assistant-avgangsplan.md` §3, §4, §9, §11, §14. The spec wins over general HA habits when they conflict; flag any conflict.

## Scope

Integration plumbing only. Planner math, schedule parsing, notification policy and privacy have their own review skills; hand off rather than duplicate.

## Checklist

### Event loop and I/O
- No blocking calls in the event loop: no `requests`, sync file I/O, `time.sleep` or heavy ICS parsing outside `hass.async_add_executor_job`.
- HTTP uses the shared session from `async_get_clientsession`, with an explicit timeout (spec default 10 s).
- Concurrency is bounded (about two external calls). One lock per profile and single-flight per cache key.
- 429 respects `Retry-After`. Backoff has jitter. Auth errors raise reauth or a Repairs issue, not tight retries.

### Config entry and flows
- One config entry per household with a set of user-defined profiles using stable slug IDs derived from the profile name (e.g. `kid_a`, `kid_b`, `parent_a`, `parent_b`). Display names are never keys, and renaming a profile must not change its id or regenerate entities.
- Persistent profile settings change only in the options flow. Entities cover per-day overrides and the notification switch only (§4.1). Reject any entity that writes a persistent setting.
- SL settings stay available for car-default profiles such as Parent B (§4.3).
- Secrets (ICS URLs) are entered as secret fields and never echoed in flow descriptions, titles or errors.
- `async_migrate_entry` exists from version 1. Store data has its own version and migration.
- Options changes trigger a reload or targeted recompute. They never leave stale timers behind.

### Coordinator and data flow
- `DataUpdateCoordinator` or an equivalent is used where it fits. One failing profile or source does not mark the whole entry unavailable (§15).
- A late response for an old config or plan revision cannot overwrite a newer plan (§11.2).
- Polling follows the windows in §11.1. There is no per-minute network traffic per profile, and `static` plans make no network calls.

### Entities
- Unique IDs are stable and derived from entry ID + profile ID + key, not from names. Each profile has a device.
- Timestamp sensors return an aware `datetime` or `None`, never a fake zero time.
- Attributes do not contain raw API responses, ICS URLs, full schedules or precise positions (§9). Attributes do not change every second, so recorder is not flooded.
- Entity set matches §9, including the packing-list sensors and button. Flag entities that §9 does not list.

### Timers, lifecycle, unload
- Timers use `async_track_point_in_utc_time` or similar. Every callback is cancelled on replan, unload and reload (§11.3).
- Callbacks re-check `mission_id`, revision and `MissionState` before acting.
- `async_unload_entry` removes listeners, timers, services (if last entry) and clears active notifications. Calling reload twice produces no duplicate listeners.

### Services
- Services in §14 are registered with voluptuous schemas and listed in `services.yaml` with translations.
- `person_id`, date key and plan revision are validated. `arrival_time` is combined with the given local date in `Europe/Stockholm`.
- `get_alternatives` uses `SupportsResponse`. Services never accept arbitrary URLs or actions.

### Manifest and packaging
- `manifest.json` contains domain, version, `config_flow: true`, real documentation and issue URLs (none invented), `iot_class` and pinned `requirements`.
- `hacs.json` is present. The minimum HA version is documented in README.

## Output

For each finding give `severity` (blocker / major / minor / nit), `file:line`, what is wrong, why (cite spec § or HA rule), and the suggested fix. End with a short verdict: approve, approve with fixes, or request changes. Report what you could not verify, for example behaviour that is only testable on the Green.
