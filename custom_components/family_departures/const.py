"""Constants for the Family Departures integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "family_departures"

# Entity platforms registered for each profile (T14, spec §9).
PLATFORMS: Final[list[str]] = [
    "binary_sensor",
    "button",
    "select",
    "sensor",
    "switch",
]

# ---------------------------------------------------------------------------
# Config entry layout (T12, spec §4.1)
# ---------------------------------------------------------------------------
# ``entry.data`` holds the household origin and the per-profile ICS URLs only.
# Secret ICS URLs never go in ``entry.options`` (review-privacy-security, §4.2).
DATA_HOUSEHOLD_NAME: Final = "household_name"
DATA_HOME_LAT: Final = "home_lat"
DATA_HOME_LON: Final = "home_lon"
# Mapping of profile id -> secret ICS URL, stored only in entry.data.
DATA_ICS_URLS: Final = "ics_urls"

# ``entry.options`` holds everything non-secret: the profiles and global flags.
OPT_PROFILES: Final = "profiles"
OPT_DRY_RUN: Final = "dry_run"
OPT_TEST_RECIPIENT_SCRIPT: Final = "test_recipient_script"

# Travel modes offered in the UI (spec §2).
MODE_PUBLIC_TRANSPORT: Final = "public_transport"
MODE_CAR: Final = "car"
MODE_STATIC: Final = "static"
MODES: Final = (MODE_PUBLIC_TRANSPORT, MODE_CAR, MODE_STATIC)

# Notification channel keys shown in the options flow. Each maps to an optional
# ``script.*`` entity so a profile without a phone can be configured (§4.1).
NOTIFICATION_CHANNELS: Final = ("push", "tts", "family_chat")

# ---------------------------------------------------------------------------
# Dispatcher (T18, spec §12.2, §12.3)
# ---------------------------------------------------------------------------
# Hook event fired for every intent so users can build their own automations;
# never used for delivery and carries no secrets (spec §12.2).
EVENT_NOTIFICATION: Final = "family_departures_notification"

# The companion app's action event and the action-id prefixes carried in the
# push buttons (spec §12.3). The id layout is ``<PREFIX>|<mission_id>|<nonce>``.
MOBILE_APP_ACTION_EVENT: Final = "mobile_app_notification_action"
ACTION_DEPARTED: Final = "FD_DEPARTED"
ACTION_PACKED: Final = "FD_PACKED"
ACTION_OFF: Final = "FD_OFF"

# Per-channel script call timeout so one slow or stuck channel cannot block the
# others or the dashboard (spec §12.2).
CHANNEL_TIMEOUT_SECONDS: Final = 15

# Default margins (minutes). Arrival default differs for adults vs children
# (spec §4.3); the others follow the §4.2 example starting values.
DEFAULT_ARRIVAL_MARGIN_CHILD: Final = 5
DEFAULT_ARRIVAL_MARGIN_ADULT: Final = 0
DEFAULT_DEPARTURE_MARGIN: Final = 5
DEFAULT_BOARDING_MARGIN: Final = 2
DEFAULT_PARKING_WALK_MARGIN: Final = 0
DEFAULT_MIN_TRANSFER_MARGIN: Final = 5
DEFAULT_CHANGE_THRESHOLD_MINUTES: Final = 3

# ---------------------------------------------------------------------------
# Diagnostics and repairs (T19, spec §15)
# ---------------------------------------------------------------------------
# A source that fails to fetch this many times in a row raises a Repairs issue
# so the household notices a feed that is persistently down (spec §15).
FETCH_FAILURE_THRESHOLD: Final = 3

# Repairs issue ids/translation keys. The concrete issue id is suffixed with the
# profile id so one broken source does not hide another (spec §15: one profile's
# problem must not take down the whole entry).
ISSUE_INVALID_SOURCE: Final = "invalid_source"
ISSUE_MISSING_DESTINATION: Final = "missing_destination"
ISSUE_REPEATED_FETCH_FAILURE: Final = "repeated_fetch_failure"
