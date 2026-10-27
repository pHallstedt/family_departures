# Family Departures

A Home Assistant custom integration (domain `family_departures`) that works out
when each family member needs to leave home to reach the day's first lesson or
work on time, picks and watches concrete SL journeys, reads traffic-based car
travel time and uses a configured static time for bike/walk, and sends
departure and packing reminders. Home Assistant provides the UI, presence and
notification channels. See `home-assistant-avgangsplan.md` for the full
specification and `docs/implementation-plan.md` for the build plan.

> Status: v1 (spec Steps 0–3) plus multiple calendars and activity missions,
> shared calendars, per-entry overrides, notification copies and school lunch.
> The integration computes plans, exposes entities and services, and drives
> notifications through your own channel scripts.

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
2. **Locations (options flow → "Locations").** Add the places you travel to
   (school, sports hall, swimming pool) with a name, coordinates and optional
   aliases. A profile's default destination is one of them, and a calendar entry
   can point at another.
3. **Calendars (options flow → "Calendars").** Add each calendar once, either as
   an ICS link (Vklass / SchoolSoft; stored as a secret, validated by a test
   fetch) or a Home Assistant calendar entity (for example a Local Calendar).
   Several profiles can link the same calendar; it is fetched once per day and
   shared.
4. **Profiles (options flow → "Add or edit a profile").** Create one profile per
   family member you want tracked. There is no fixed set of profiles; add as
   many as you need and give each one a name. Each profile gets a stable slug id
   derived from its name (for example `kid_a`, `parent_b`), so renaming a
   profile later does not regenerate its entities. For each profile configure:
   - **Name and role:** the display name and whether the profile is an adult
     (`is_adult`), which selects the default margins.
   - **Destination and travel mode:** the default location and default mode
     (public transport / car / static). Public-transport margins stay editable
     even when the default mode is car. Set static minutes/label for bike/walk,
     and a conservative car fallback time.
   - **Calendars and links:** tick the calendars the profile follows and choose
     the **main** one (the school or workplace: the first event of the day sets
     the departure). Every other linked calendar is an **activity calendar**:
     each of its events becomes its own trip, a *mission*, with its own reminder
     and "go now" notice. Each link has its own label, include/exclude patterns,
     which events apply to this person, an optional default location, mode and
     margin, and per-summary **rules** (see below). A profile with no calendar is
     fine, for example a child who only receives copies.
   - **Margins and expected days:** arrival/departure/boarding/parking/transfer
     margins (defaults: children 5 min arrival, adults 0) and the weekday mask.
   - **Packing rule:** an optional case-insensitive summary substring that adds
     a packing item (for example a PE lesson → "Gympakläder"), with a preview of
     the next matching dates.
   - **Notifications and channels:** quiet hours, change threshold, the person
     entity for presence, copies, lunch and the per-channel scripts. Scripts are
     optional, so a child profile can be fully configured before that child has
     a phone.
   - **Test calculation:** the final step shows a sample calculation (car and
     transit) so you can sanity-check the profile.
5. **Holidays and days off.** Create a shared Local Calendar and add all-day
   "Ledig" events for holidays and school breaks, or set a per-day override. An
   empty school feed is reported as "Inget schema registrerat", not as a day
   off.

### Several calendars and activities

- **Main versus activity.** The main calendar keeps the "first event of the day"
  behaviour. Activity calendars are never merged into it: football at 17:30 is a
  separate mission with its own departure time, titled
  `Name – Fotboll 17:30`.
- **Shared calendars.** One family calendar can serve several people. On each
  link, *which events apply* is `all`, `tagged` (only entries naming this person)
  or `tagged_or_untagged` (entries naming this person plus entries naming nobody).
  Joining a calendar that another profile already uses switches the others'
  activity links from `all` to `tagged_or_untagged`, so nobody suddenly gets
  everyone's events.
- **Overrides on a calendar entry.** Put one `key: value` per line in the entry's
  description (or `[key: value]` in the title if your app makes the description
  awkward):

  | Key (aliases) | Meaning | Example |
  | --- | --- | --- |
  | `plats`, `location`, `destination` | where the entry takes place; a location name, id or alias | `plats: Badhuset` |
  | `färdsätt`, `transport`, `mode` | `kollektivt`, `bil`, `cykel`/`gå`, ... | `färdsätt: bil` |
  | `vem`, `who` | who it applies to, comma separated; `alla` means everyone | `vem: Kid A, Kid B` |

  Unknown keys and the rest of the description are ignored and never stored.
  Overrides beat the link's **rules**, which beat the link default, which beats
  the profile default.
- **Rules per link.** One rule per line, `<summary text> => <place>[, <mode>]`:
  `IDRO => Sporthallen` or `Simning => Badhuset, kollektivt`. The match is a
  case-insensitive substring of the title.
- **Warnings.** A mission that leaves too little time at home after the previous
  one shows `⚠ kort tid hemma`; overlapping missions show `⚠ krockar med annat`,
  in the notices, on the sensor and in `reasons`.
- **Removed entries.** If a calendar entry for today disappears, the profile gets
  a short "removed" notice and the stale notification on the phone is cleared.
  Not when the day is off, or when the calendar simply failed to load.

### Evening summary

At 20:00 each profile gets one notice for tomorrow listing every mission in
start order with its departure time, then the packing list and, if configured,
the lunch. It is on by default; when off, the profile only gets a packing notice
if there is something to pack. Tomorrow's preliminary plan and the
`packing_list_tomorrow` sensor are computed from 18:00. Tomorrow only ever sends
that evening (or packing) notice; change alerts for a trip start on the trip's
own day. Quiet hours hold it; "Packat" acts on the digest's date.

### Copies to other profiles

On the **Notifications** step a profile can *send copies to* other profiles
(typically a parent being told what a child was told) and choose which notice
types to copy. Copies:

- go to the recipient's own push script, titled `[Kopia] ...`, without buttons,
  Live Update or countdown;
- are only sent while the *source* profile's notifications are enabled, even if
  the source has no phone;
- are never copied again (A→B plus B→A gives each exactly one copy);
- are dropped inside the recipient's quiet hours, except `critical` and `cleanup`;
- always include the `cleanup` that clears a copied notice once any kind is
  copied.

No positions are shared, only the notice text.

### Lunch

Set **Lunch calendar entity** on the Notifications step to a calendar with one
event per day whose title is the menu. The HACS
[`skolmat`](https://github.com/Kaptensanders/skolmat) integration provides one
for skolmaten.se and several other menu systems; this integration adds no
outbound host for it. Dishes are split on line breaks and ` | `.

- The sensor `lunch_tomorrow` (only created for a profile with a lunch calendar)
  shows tomorrow's dishes, with `date`, `dishes` and `status` attributes. It is
  empty on a weekend, a holiday or when the calendar can't be read.
- The evening summary adds `Lunch imorgon: ...` when tomorrow has a school
  (main) mission and a menu. Turn this off with **Include lunch in the evening
  summary**. A failing lunch source never delays the summary.
- Menus are cached for 3 hours (30 minutes after an error), and profiles sharing a
  lunch calendar share one lookup.

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
  day overrides and action buttons, a missions-today card, a separate "tomorrow"
  view with the evening packing lists, tomorrow's missions and lunch, and a
  diagnostics view. It shows distinct messages for "Inget
  schema registrerat", "Ledig" and "Schemakälla kunde inte hämtas".
- `examples/scripts.yaml` – example channel scripts, including an actionable
  Android push with optional Live Update and a privacy-mode variant. Copy them,
  point `notify.mobile_app_...` at your real phones, and map them per profile.

## Dry-run and shadow mode

Before trusting live reminders, run the integration in shadow mode:

1. The global **dry run** option is **on by default**, also when you have never
   saved Configure → Settings. The dispatcher then only logs and fires the
   `family_departures_notification` event; no channel scripts are called. While
   it is on, setup logs one INFO line saying so. To go live, open Configure →
   Settings and turn dry run off.
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
- Lunch comes from a Home Assistant calendar entity such as `skolmat`; there is
  no built-in skolmaten.se client (it needs a client token this project does not
  own). "Lunch idag" in the morning notice is not implemented.
- The main calendar only drives the first event of the day. Extra events in the
  same school calendar (a return trip after lunch) belong in an activity
  calendar.
- The lunch calendar is read per day: an all-day event spanning several days
  (a holiday entry in a general calendar) shows as the menu on each of them.
  `skolmat` writes one event per day, so it is not affected.
- A Home Assistant calendar gives no event UID, so a mission is keyed by the
  event title: renaming an entry makes a new mission, and moving one of several
  same-titled entries past another can swap their notification history.
- Extra missions more than four hours away are re-planned at most every 30
  minutes, so a travel-time change for a late activity shows up gradually.
- "An adult usually has the car but takes transit on specific days" is still
  done with the per-day transport override.

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
