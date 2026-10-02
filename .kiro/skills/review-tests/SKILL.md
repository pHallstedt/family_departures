---
name: review-tests
description: Review the test suite of the family departures integration - coverage of the spec scenario table, controlled time, sanitized fixtures, provider contract tests, HA integration tests with pytest-homeassistant-custom-component, and whether tests assert product behaviour rather than mirror implementation. Use when reviewing tests/ or before declaring a build step done.
---

# Review: tests and acceptance

Spec: `home-assistant-avgangsplan.md` §16 (scenario table and acceptance) and §17 (done criteria per step).

## Checklist

### Coverage against spec
- Map every row of the §16 scenario table to a test. List any row that has no test, citing the row text. Scenarios for features not yet built (separate realtime source, v1.1 channels) may be skipped, but must be marked as skipped.
- Check the "Klart när" criterion for the current step in §17 and whether tests demonstrate it.

### Test quality
- Tests assert user-visible behaviour (times, status, which notifications are sent), not internal call sequences. Flag tests that only restate the implementation.
- Time is controlled with `freezegun` or HA's `async_fire_time_changed`, with `now` injected into pure modules. No test depends on the wall clock or the machine time zone.
- DST: at least one test each on the last Sunday of March and of October in `Europe/Stockholm`, including SchoolSoft's `Europe/Berlin` TZID.
- Planner tests use concrete clock times and check exact minutes, including margin stacking.
- Notification tests cover dedup across restart, quiet-hours shift, cumulative small changes, stale action, and one channel failing.
- Edge cases: Waze error, empty list or string value; missing realtime; cancelled leg 2; no on-time journey; old response after a mode switch.

### Fixtures
- Fixtures are sanitised (see the review-privacy-security skill). Contain real-world structure: SchoolSoft `Lektion <code>` summaries, `LUNCH`, Berlin TZID, a full-year span trimmed to a few weeks; Vklass with whatever event types Step 0 found.
- Provider contract tests use locked response samples. Live API smoke tests are opt-in only (env flag) and never run in CI by default.

### HA integration tests
- They use `pytest-homeassistant-custom-component` pinned to the Green's HA version.
- Covered: config flow validation and errors, options plus reload, entity registry stability across rename, service schemas, Store migration, unload leaving no timers or listeners (check `hass` for lingering timers).

### Hygiene
- Tests are deterministic and fast, with no network by default. Test names describe the behaviour in plain words.

## Output

1. A coverage table with columns: §16 scenario, test name or "MISSING", and notes.
2. Findings with `severity`, `file:line`, the problem and a fix.
3. A verdict on whether the current §17 step's done criteria are demonstrated by tests.

Run the suite (`pytest -q`) if possible and report the actual result. If it cannot run, say why.
