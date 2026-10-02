"""Notification dispatcher and companion-app action handling (spec §12.2–§12.4).

The scheduler (T17) owns *timing* and the ledger and hands this module a batch
of :class:`NotificationIntent` objects to *deliver*. This module is the only
place that turns an intent into an outbound action, so it is where the delivery
rules live:

* For each intent and each of its channels, call ``script.turn_on`` on the
  script the options flow mapped to that profile/channel, passing the intent as
  the script's variables (spec §12.2). Targets come only from that allowlist,
  never from fields inside the intent.
* Each channel is called separately, with its own timeout, and one channel
  failing (or a missing script) never stops the other channels or the dashboard
  (spec §12.2). A profile with no script for a channel is skipped silently
  (logged at debug), not an error.
* The global **dry-run** setting logs the intent and fires the hook event but
  calls no script at all (spec §12.2).
* The global **test recipient** redirects *every* profile's intents to one
  script (a single developer phone during development): the profile's own
  scripts are never called, the title is prefixed ``[TEST <Name>]`` and the
  Android ``tag`` gets the person id appended so profiles do not overwrite each other's
  notifications. The action buttons still carry the *real* person's mission id
  and nonce so a tap confirms the right mission (spec §12.2, Environment facts).
* The ``family_departures_notification`` event is always fired with the intent's
  content as a hook for user automations; it is never used for delivery and
  carries no secrets (spec §12.2).

It also listens to the companion app's ``mobile_app_notification_action`` event
and parses the action ids the push buttons carry
(``FD_DEPARTED|<mission_id>|<nonce>`` etc., spec §12.3). An action is applied
only when its mission still exists and its nonce matches the mission's stored
``action_nonce``, so yesterday's "Jag går nu" does nothing today.

This module imports ``homeassistant`` (it calls services, fires events and
subscribes to the bus) and is therefore not pure; all content decisions were
already made by the pure policy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date

from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_DEPARTED,
    ACTION_OFF,
    ACTION_PACKED,
    CHANNEL_TIMEOUT_SECONDS,
    EVENT_NOTIFICATION,
    MOBILE_APP_ACTION_EVENT,
)
from .models import (
    DayOverride,
    MissionState,
    NotificationIntent,
    ProfileConfig,
)
from .store import FamilyDeparturesStore

_LOGGER = logging.getLogger(__name__)

# The ``script`` integration's domain; a literal avoids importing the component
# (which does not export ``DOMAIN``) and matches how the providers name domains.
SCRIPT_DOMAIN = "script"

# Callback giving the dispatcher the current profiles keyed by person id. Kept
# as an indirection (rather than a stored dict) so a reconfigure is reflected
# without rebuilding the dispatcher, mirroring the scheduler.
ProfileLookup = Callable[[], Mapping[str, ProfileConfig]]

# Callbacks reading the two global toggles live from the config entry so an
# options change (which reloads the entry) always takes effect.
BoolLookup = Callable[[], bool]
ScriptLookup = Callable[[], str | None]

# Dispatcher that triggers a coordinator recompute after an action changed state
# (e.g. a confirmed departure must close the mission). Optional so tests can
# drive the dispatcher without a coordinator.
RefreshCallback = Callable[[], object]


class NotificationDispatcher:
    """Delivers intents to channel scripts and applies companion actions."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: FamilyDeparturesStore,
        profiles: ProfileLookup,
        *,
        dry_run: BoolLookup,
        test_recipient: ScriptLookup,
        request_refresh: RefreshCallback | None = None,
    ) -> None:
        self._hass = hass
        self._store = store
        self._profiles = profiles
        self._dry_run = dry_run
        self._test_recipient = test_recipient
        self._request_refresh = request_refresh
        self._cancel_action_listener: Callable[[], None] | None = None

    # -- Lifecycle ----------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Subscribe to the companion-app action event (spec §12.3)."""
        if self._cancel_action_listener is not None:
            return
        self._cancel_action_listener = self._hass.bus.async_listen(
            MOBILE_APP_ACTION_EVENT, self._handle_action_event
        )

    @callback
    def async_shutdown(self) -> None:
        """Unsubscribe so a reload leaves no dangling listener."""
        if self._cancel_action_listener is not None:
            self._cancel_action_listener()
            self._cancel_action_listener = None

    # -- Delivery (the scheduler's DispatchCallback) ------------------------

    async def async_dispatch(self, intents: list[NotificationIntent]) -> None:
        """Deliver a batch of intents (matches the scheduler's callback)."""
        for intent in intents:
            await self._async_dispatch_one(intent)

    # Allow using the dispatcher directly as the scheduler's callback.
    __call__ = async_dispatch

    async def _async_dispatch_one(self, intent: NotificationIntent) -> None:
        """Fire the hook event and call every channel script for one intent."""
        # The hook event is always fired with the same content, even in dry-run,
        # and never carries a secret (spec §12.2).
        self._hass.bus.async_fire(EVENT_NOTIFICATION, _event_data(intent))

        if self._dry_run():
            _LOGGER.info(
                "Dry-run: notification %s for %s (%s) not sent",
                intent.kind,
                intent.person_id,
                intent.mission_id,
            )
            return

        for script_entity, variables in self._targets(intent):
            await self._async_call_channel(script_entity, variables, intent)

    def _targets(
        self, intent: NotificationIntent
    ) -> list[tuple[str, dict[str, object]]]:
        """Resolve (script_entity, variables) pairs for an intent.

        In test-recipient mode every intent goes to the single test script with
        a ``[TEST <Name>]`` title and a per-person tag; otherwise each of the
        intent's channels maps to the profile's configured script, skipping
        channels with no script (spec §12.2).
        """
        profile = self._profiles().get(intent.person_id)
        name = profile.name if profile is not None else intent.person_id

        test_script = self._test_recipient()
        if test_script:
            return [(test_script, _test_variables(intent, name))]

        if profile is None:
            return []
        pairs: list[tuple[str, dict[str, object]]] = []
        for channel in intent.channels:
            script_entity = profile.scripts.get(channel)
            if not script_entity:
                _LOGGER.debug(
                    "No script for channel %s of %s; skipping",
                    channel,
                    intent.person_id,
                )
                continue
            pairs.append((script_entity, _variables(intent, channel)))
        return pairs

    async def _async_call_channel(
        self,
        script_entity: str,
        variables: dict[str, object],
        intent: NotificationIntent,
    ) -> None:
        """Call one channel script with its own timeout and isolation.

        A timeout, a missing script or any error from one channel is logged and
        swallowed so the remaining channels and the dashboard are unaffected
        (spec §12.2). The host-only log never includes secret-bearing fields.
        """
        try:
            async with asyncio.timeout(CHANNEL_TIMEOUT_SECONDS):
                await self._hass.services.async_call(
                    SCRIPT_DOMAIN,
                    SERVICE_TURN_ON,
                    {ATTR_ENTITY_ID: script_entity, "variables": variables},
                    blocking=True,
                )
        except TimeoutError:
            _LOGGER.warning(
                "Channel script %s timed out for %s notification",
                script_entity,
                intent.kind,
            )
        except Exception:  # noqa: BLE001 - one channel must never break others
            _LOGGER.exception(
                "Channel script %s failed for %s notification",
                script_entity,
                intent.kind,
            )

    # -- Companion-app actions (spec §12.3) ---------------------------------

    @callback
    def _handle_action_event(self, event: Event) -> None:
        """Parse and apply a ``mobile_app_notification_action`` event.

        The companion app fires this with the pressed button's ``action`` id.
        Our ids are ``<PREFIX>|<mission_id>|<nonce>``; anything else (another
        integration's action, a malformed id) is ignored.
        """
        raw = event.data.get("action")
        if not isinstance(raw, str):
            return
        parsed = _parse_action(raw)
        if parsed is None:
            return
        prefix, mission_id, nonce = parsed

        state = self._store.get_mission(mission_id)
        if state is None or state.action_nonce != nonce:
            # Unknown mission or a stale nonce (e.g. yesterday's notification):
            # a no-op by design (spec §12.3).
            _LOGGER.debug("Ignoring stale/unknown action for %s", mission_id)
            return

        if self._apply_action(prefix, mission_id, state):
            self._maybe_request_refresh()

    def _apply_action(self, prefix: str, mission_id: str, state: MissionState) -> bool:
        """Apply a validated action to the store; return True if state changed."""
        person_id, local_date = _mission_person_date(mission_id)
        if person_id is None or local_date is None:
            return False

        if prefix == ACTION_DEPARTED:
            if state.status == "departed":
                return False
            self._store.set_mission(
                replace(
                    state,
                    status="departed",
                    departed_at=dt_util.utcnow(),
                    reopened=False,
                )
            )
            return True

        if prefix == ACTION_OFF:
            base = self._store.get_override(person_id, local_date) or DayOverride(
                person_id=person_id, local_date=local_date
            )
            self._store.set_override(replace(base, attendance="off"))
            return True

        if prefix == ACTION_PACKED:
            self._acknowledge_packing(person_id, local_date)
            return True

        return False

    def _acknowledge_packing(self, person_id: str, local_date: date) -> None:
        """Acknowledge the whole current packing list for its own date (§5.5).

        The mission id encodes the person and the date, so a "Packat" tap only
        ever acknowledges that date's list; an evening notice therefore
        acknowledges tomorrow's list and a morning notice today's.
        """
        profile = self._profiles().get(person_id)
        if profile is None:
            return
        schedule = self._store.get_schedule(profile.destination_id, local_date)
        events = schedule.events if schedule is not None else ()
        # Lazy import keeps packing's purity boundary and avoids an import cycle.
        from .packing import build_packing_list

        override = self._store.get_override(person_id, local_date)
        already = self._store.get_packing_acks(person_id, local_date)
        packing = build_packing_list(
            person_id, local_date, events, profile.packing_rules, override, already
        )
        # Everything currently shown becomes acknowledged; keep prior acks too.
        acknowledged = tuple(dict.fromkeys((*already, *packing.items)))
        self._store.set_packing_acks(person_id, local_date, acknowledged)

    def _maybe_request_refresh(self) -> None:
        """Ask the coordinator to recompute after an action, if wired."""
        if self._request_refresh is None:
            return
        result = self._request_refresh()
        if asyncio.iscoroutine(result):
            self._hass.async_create_task(result)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _event_data(intent: NotificationIntent) -> dict[str, object]:
    """The hook-event payload: intent content, no secrets (spec §12.2)."""
    return _variables(intent, channel=None)


def _variables(intent: NotificationIntent, channel: str | None) -> dict[str, object]:
    """Script/event variables for an intent (spec §12.2 variable list).

    Times are serialised as ISO-8601 UTC strings (or ``None``); nothing here is
    a secret (no URLs, tokens, coordinates or raw calendar text).
    """
    return {
        "person_id": intent.person_id,
        "plan_id": intent.plan_id,
        "revision": intent.revision,
        "notification_id": intent.notification_id,
        "kind": intent.kind,
        "severity": intent.severity,
        "title": intent.title,
        "message": intent.message,
        "recommended_leave_time": _iso(intent.recommended_leave_time),
        "latest_leave_time": _iso(intent.latest_leave_time),
        "quality": intent.quality,
        "reason_codes": list(intent.reason_codes),
        "packing_items": list(intent.packing_items),
        "channels": list(intent.channels),
        "tag": intent.tag,
        "action_nonce": intent.action_nonce,
    }


def _test_variables(intent: NotificationIntent, name: str) -> dict[str, object]:
    """Variables for test-recipient mode: prefixed title, per-person tag.

    The action buttons still carry the real person's mission id and nonce
    (unchanged ``action_nonce`` and ``mission_id`` via ``person_id``), so a tap
    on the test phone confirms the correct mission (spec §12.2).
    """
    variables = _variables(intent, channel=None)
    variables["title"] = f"[TEST {name}] {intent.title}"
    # Append the person id so four profiles routed to one phone do not replace
    # each other's notifications (spec §12.2).
    variables["tag"] = f"{intent.tag}_{intent.person_id}"
    return variables


def _iso(value: object) -> str | None:
    """ISO-8601 string for an aware datetime, or ``None``."""
    from datetime import datetime

    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _parse_action(raw: str) -> tuple[str, str, str] | None:
    """Parse ``<PREFIX>|<mission_id>|<nonce>`` for our known prefixes.

    Returns ``(prefix, mission_id, nonce)`` or ``None`` for a foreign/malformed
    id. The mission id itself contains colons, not pipes, so a plain
    three-field split is unambiguous.
    """
    parts = raw.split("|")
    if len(parts) != 3:
        return None
    prefix, mission_id, nonce = parts
    if prefix not in (ACTION_DEPARTED, ACTION_PACKED, ACTION_OFF):
        return None
    if not mission_id or not nonce:
        return None
    return prefix, mission_id, nonce


def _mission_person_date(mission_id: str) -> tuple[str | None, date | None]:
    """Split ``person:YYYY-MM-DD:slot`` into person id and local date (§8)."""
    parts = mission_id.split(":")
    if len(parts) < 2:
        return None, None
    person_id = parts[0]
    try:
        local_date = date.fromisoformat(parts[1])
    except ValueError:
        return None, None
    return person_id, local_date


__all__ = ["NotificationDispatcher"]
