---
name: review-privacy-security
description: Review privacy and security of the family departures integration - secret ICS tokens, logs, diagnostics, entity attributes, fixtures, outbound requests, notification content, action validation and children's data. Use on any change touching config storage, logging, diagnostics.py, fixtures, HTTP fetching, notification payloads or before committing.
---

# Review: privacy and security

Spec: `home-assistant-avgangsplan.md` §9, §12.3, §12.4, §15, §16.

This system handles children's schedules, home and school locations, and personal calendar tokens. Treat leaks as **blocker** severity.

## Checklist

### Secrets
- The Vklass and SchoolSoft ICS URLs contain personal tokens. They are stored only in the config entry and never appear in entity state or attributes, logs (including exception messages and `repr` of URLs or requests), diagnostics, Repairs text, fixtures, README, the spec or git.
- Run a repo-wide search for `schoolsoft.se/.*/ical-feed/`, `vklass` URLs with query tokens, and long random path segments. Any hit is a blocker.
- HTTP errors are logged without the full URL; log the host only.
- Remind the user that config entries and backups are not encrypted secret storage (§15).

### Diagnostics and storage
- `diagnostics.py` redacts names, exact coordinates, ICS tokens, API keys and calendar content (`async_redact_data`).
- Store holds only today's and tomorrow's normalised events, with a bounded cache. The ledger is pruned after about 7 days. Raw realtime feeds are not persisted.
- Recorder is not flooded with per-minute journey history.

### Outbound data
- Routing requests (SL, Waze) contain home and destination coordinates only: no person names, lesson titles or children's live positions (§12.4).
- Only the configured hosts are fetched. Redirects are validated and response size is capped. `LOCATION` from a calendar is never used as a network target or geocoded in v1.
- No dependency sends data elsewhere. New dependencies are pinned and well known; flag unfamiliar packages, including the future `skolmat` HACS integration.

### Actions and input validation
- Service calls and notification actions validate `person_id`, date, mission and revision. The action nonce is bound to the mission and phone. Stale actions are no-ops.
- Nothing from calendar entries (URLs, actions, targets) ends up in notification buttons or script targets.
- Dispatch targets come only from the allowlist.

### Notification content
- Privacy mode ("Dags att gå, öppna HA för detaljer") is available. Calendar details are not read aloud late in the evening.
- Family chat sharing is explicit in the UI, and there is no automatic sharing of children's positions.

### Accounts (§10)
- Documentation states that non-admin HA accounts can still control every entity. The children get a dedicated dashboard. Nothing in the code relies on HA user roles for protection.

### Fixtures and tests
- Fixtures are sanitised: course codes may remain, while UID, DESCRIPTION, teacher, room, names and addresses are anonymised. There are no real coordinates.

## Output

Per finding: `severity`, `file:line`, the data at risk, the leak path, and the fix. Never quote the secret itself in the review; refer to it by location and type. State explicitly which checks were done by search and which by reading code.
