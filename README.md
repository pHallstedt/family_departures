# Family Departures

A Home Assistant custom integration (domain `family_departures`) that works out
when each family member needs to leave home to reach the day's first lesson or
work on time, picks and watches concrete SL journeys, reads traffic-based car
travel time and uses a configured static time for bike/walk, and sends
departure and packing reminders. Home Assistant provides the UI, presence and
notification channels. See `home-assistant-avgangsplan.md` for the full
specification and `docs/implementation-plan.md` for the build plan.

> Status: v1 (spec Steps 0–3). The integration computes plans, exposes entities
> and services, and drives notifications through your own channel scripts.

## Requirements

- Home Assistant Core **2026.9.4** (tested). Minimum **2026.9.0** (see
  `hacs.json` / `manifest.json`); older versions are untested.
- Python **>= 3.14.2** (bundled with that HA Core).
- Live Update push rendering needs Core 2026.7+ and the Android Companion app
  on Android 16+. Everything else works without it.
- The Waze travel time integration (`waze_travel_time`) must be available for
  car mode. SL public-transport planning uses Trafiklab's Journey Planner v2 and
  needs no API key at the time of writing.

## Install (HACS custom repository)

1. In HACS, add this repository as a **custom repository** of type
   *Integration*.
2. Install **Family Departures** from HACS, then restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for
   *Family Departures*.

Manual install: copy `custom_components/family_departures/` into your HA
`config/custom_components/` directory and restart.

## Setup walkthrough

1. **Household (config flow).** Enter a household name and your home location
   (map selector). This creates one config entry.
2. **Profiles (options flow → "Add or edit a profile").** Create one profile per
   family member you want tracked. There is no fixed set of profiles; add as
   many as you need and give each one a name. Each profile gets a stable slug id
   derived from its name (for example `kid_a`, `kid_b`, `parent_a`, `parent_b`),
   so renaming a profile later does not regenerate its entities. For each
   profile configure:
   - **Name and role:** the display name and whether the profile is an adult
     (`is_adult`), which selects the default margins.
   - **Source:** either an ICS link (Vklass / SchoolSoft; stored as a secret,
     validated by a test fetch) or a Home Assistant calendar entity (for example
     a Local Calendar for an adult). Optional include/exclude summary patterns.
   - **Destination and travel mode:** destination location, default mode
     (public transport / car / static). Public-transport margins stay editable
     even when the default mode is car. Set static minutes/label for bike/walk,
     and a conservative car fallback time.
   - **Margins and expected days:** arrival/departure/boarding/parking/transfer
     margins (defaults: children 5 min arrival, adults 0) and the weekday mask.
   - **Packing rule:** an optional case-insensitive summary substring that adds
     a packing item (for example a PE lesson → "Gympakläder"), with a preview of
     the next matching dates.
   - **Notifications and channels:** quiet hours, change threshold, the person
     entity for presence, and the per-channel scripts. Scripts are optional, so
     a child profile can be fully configured before that child has a phone.
   - **Evening summary:** on by default. When off, the profile gets a
     packing-only notice at 20:00 instead of the full evening summary.
   - **Test calculation:** the final step shows a sample calculation (car and
     transit) so you can sanity-check the profile.
3. **Holidays and days off.** Create a shared Local Calendar and add all-day
   "Ledig" events for holidays and school breaks, or set a per-day override. An
   empty school feed is reported as "Inget schema registrerat", not as a day
   off.

Persistent profile settings (default mode, travel times, margins) change
**only** through the options flow. The day selects and switches are for per-day
overrides and the notification on/off toggle.

## Companion app and presence

1. Install the Home Assistant **Companion app (Android)** with a separate HA
   account for each family member.
2. Create a `person.<name>` for each profile (for example `person.kid_a`,
   `person.parent_a`) and link each to the right phone's device tracker. Pair
   the phone manually; do not guess the device from a name.
3. Set up the Home zone, grant location permission, enable background updates
   and exclude the Companion app from battery optimisation. Test on real
   Wi-Fi/mobile data.
4. Map the `notify.mobile_app_*` target for each phone into a channel script
   (see `examples/scripts.yaml`) and assign it to the profile's push channel.

## Example dashboard and scripts

- `examples/dashboard.yaml` – a standard-card dashboard: a family overview
  sorted by the next recommended departure, a detail card per person with the
  day overrides and action buttons, a separate "tomorrow" view with the evening
  packing lists, and a diagnostics view. It shows distinct messages for "Inget
  schema registrerat", "Ledig" and "Schemakälla kunde inte hämtas".
- `examples/scripts.yaml` – example channel scripts, including an actionable
  Android push with optional Live Update and a privacy-mode variant. Copy them,
  point `notify.mobile_app_...` at your real phones, and map them per profile.

## Dry-run and shadow mode

Before trusting live reminders, run the integration in shadow mode:

1. Enable the global **dry run** option. The dispatcher then only logs and fires
   the `family_departures_notification` event; no channel scripts are called.
2. Set a global **test recipient** script (a single phone, e.g. your own phone).
   While it is set, every notification for every person is routed only to that
   script, titles are prefixed `[TEST <Name>]`, and the per-person tag keeps the
   profiles from overwriting each other. Each profile's own scripts are not
   called.
3. Compare the planned departures against what actually happened for about a
   week and tune the margins. The plan attributes and the dashboard expose the
   margin breakdown (travel, walk, each margin) so you can see which margin to
   trim.
4. When it looks right, clear the test recipient, install the Companion app on
   the remaining phones, and enable push for one person at a time after a test
   notification.

## Privacy and security

- **Link tokens.** Vklass and SchoolSoft ICS links contain a personal token.
  They are stored only in the config entry, never shown in entity state or
  attributes, logs, diagnostics or Repairs text. Keep real links out of the
  repository; local secrets belong in `local/` (gitignored). Note that config
  entries and backups are **not** encrypted secret storage.
- **Outbound data.** Routing requests (SL, Waze) carry home and destination
  coordinates only – no names, lesson titles or children's live positions. Only
  the configured hosts are fetched, redirects are restricted to the same host
  and response size is capped. A calendar event's `LOCATION` is never used as a
  network target.
- **Notification content.** A privacy mode ("Dags att gå, öppna HA för
  detaljer") is available so schedule details are not pushed late in the
  evening. There is no automatic sharing of children's positions, and family
  chat sharing is explicit in the UI.
- **Accounts.** Give the children **non-admin** HA accounts, but be aware that
  Home Assistant has no per-entity permissions: a non-admin user can still
  control every entity in the house. Treat the accounts as trusted family
  accounts and give the children a dedicated dashboard. Nothing in this
  integration relies on HA user roles for protection.

## Backup, rollback and uninstall

- **Backup** Home Assistant (Settings → System → Backups) before installing or
  upgrading, and before changing the configuration. Remember the backup is not
  encrypted secret storage.
- **Rollback:** restore the most recent backup, or in HACS pick an earlier
  released version of the integration and restart.
- **Uninstall:** remove the config entry (Settings → Devices & Services), then
  uninstall from HACS (or delete `custom_components/family_departures/`), and
  restart. Removing the entry deletes its entities and devices and clears the
  stored state and timers.

## Known limitations (v1)

- No automatic departure detection from presence; use the "Mark departed"
  button or the push action. Presence-based auto-departure is planned for v1.1.
- After departure, continued trip tracking is out of scope.
- No TTS or family-chat delivery beyond what your own channel scripts provide;
  richer channels are v1.1.
- Weather surcharge for static mode is a simple adjustable add-on, off by
  default, not a validated meteorological model.
- Evening and weekend activities, "an adult usually has the car but takes
  transit on specific days", and showing the school lunch in the notification
  are planned for a later version.

## Development

This project uses [uv](https://docs.astral.sh/uv/). The test harness pins
`pytest-homeassistant-custom-component==0.13.367`, which matches HA 2026.9.4.

```bash
uv run ruff check
uv run ruff format --check
uv run mypy custom_components
uv run pytest -q
```

Real ICS feed URLs, API tokens, home/school addresses and coordinates must
**never** be committed. Put local secrets in `local/` (gitignored); test
fixtures are sanitised. `tests/test_no_secrets.py` scans tracked files and fails
on anything that looks like a token or a real coordinate.
