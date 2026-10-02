"""Services for the Family Departures integration (spec §14).

Registers the seven household actions from the §14 table once per Home
Assistant instance (shared across config entries) and removes them again when
the last entry unloads:

* ``refresh`` -- a limited manual recompute, optionally for one person.
* ``mark_departed`` -- confirm departure for a mission's current plan, closing
  it so no further leave/critical pushes go out that day (spec §10).
* ``set_override`` -- write a per-day override (mode / attendance / arrival
  time) for a future-or-today date. ``arrival_time`` is combined with the
  given date in ``Europe/Stockholm`` (spec §14: not the server's UTC date).
* ``clear_override`` -- drop a per-day override.
* ``get_alternatives`` -- return response data describing the reachable
  journeys the current plan knows about (``SupportsResponse.ONLY``).
* ``select_journey`` -- record an explicitly chosen journey for a plan.
* ``reopen_today`` -- explicitly reopen a mission locked after departure
  (spec §5.3).

Validation (spec §14): ``person_id`` must name a configured profile, the date
key must parse, and ``plan_id``/``journey_id`` must match the mission's current
plan. Services never accept arbitrary URLs or actions
(review-ha-integration, spec §14).

The handlers reach the coordinator's data and its Store the same way the
entity platforms do; the pure planning modules are never imported here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .models import Attendance, DayOverride, Mode
from .timeutil import local_date_of, make_mission_id

if TYPE_CHECKING:
    from .coordinator import FamilyDeparturesCoordinator

# Service names (spec §14).
SERVICE_REFRESH = "refresh"
SERVICE_MARK_DEPARTED = "mark_departed"
SERVICE_SET_OVERRIDE = "set_override"
SERVICE_CLEAR_OVERRIDE = "clear_override"
SERVICE_GET_ALTERNATIVES = "get_alternatives"
SERVICE_SELECT_JOURNEY = "select_journey"
SERVICE_REOPEN_TODAY = "reopen_today"

# Field names shared across several schemas.
ATTR_PERSON_ID = "person_id"
ATTR_DATE = "date"
ATTR_PLAN_ID = "plan_id"
ATTR_JOURNEY_ID = "journey_id"
ATTR_MODE = "mode"
ATTR_ATTENDANCE = "attendance"
ATTR_ARRIVAL_TIME = "arrival_time"

_MODE_VALUES = ("public_transport", "car", "static")
_ATTENDANCE_VALUES = ("normal", "off", "remote", "sick")

_REFRESH_SCHEMA = vol.Schema({vol.Optional(ATTR_PERSON_ID): cv.string})

_MARK_DEPARTED_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PERSON_ID): cv.string,
        vol.Required(ATTR_PLAN_ID): cv.string,
    }
)

_SET_OVERRIDE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PERSON_ID): cv.string,
        vol.Required(ATTR_DATE): cv.date,
        vol.Optional(ATTR_MODE): vol.In(_MODE_VALUES),
        vol.Optional(ATTR_ATTENDANCE): vol.In(_ATTENDANCE_VALUES),
        vol.Optional(ATTR_ARRIVAL_TIME): cv.time,
    }
)

_CLEAR_OVERRIDE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PERSON_ID): cv.string,
        vol.Required(ATTR_DATE): cv.date,
    }
)

_GET_ALTERNATIVES_SCHEMA = vol.Schema({vol.Required(ATTR_PERSON_ID): cv.string})

_SELECT_JOURNEY_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PERSON_ID): cv.string,
        vol.Required(ATTR_PLAN_ID): cv.string,
        vol.Required(ATTR_JOURNEY_ID): cv.string,
    }
)

_REOPEN_TODAY_SCHEMA = vol.Schema({vol.Required(ATTR_PERSON_ID): cv.string})

# All service names, used when removing on the last unload.
_ALL_SERVICES = (
    SERVICE_REFRESH,
    SERVICE_MARK_DEPARTED,
    SERVICE_SET_OVERRIDE,
    SERVICE_CLEAR_OVERRIDE,
    SERVICE_GET_ALTERNATIVES,
    SERVICE_SELECT_JOURNEY,
    SERVICE_REOPEN_TODAY,
)


def _loaded_coordinators(
    hass: HomeAssistant, *, exclude_entry_id: str | None = None
) -> list[FamilyDeparturesCoordinator]:
    """Return the coordinators of every loaded config entry for the domain.

    ``exclude_entry_id`` skips a specific entry; the unload path uses it because
    the entry being torn down still reports ``LOADED`` while its unload callback
    runs, so it must not keep the shared services alive for itself.
    """
    coordinators: list[FamilyDeparturesCoordinator] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == exclude_entry_id:
            continue
        if entry.state is not ConfigEntryState.LOADED:
            continue
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is not None:
            coordinators.append(coordinator)
    return coordinators


def _find_profile(hass: HomeAssistant, person_id: str) -> FamilyDeparturesCoordinator:
    """Return the coordinator owning ``person_id`` or raise a validation error.

    One household entry holds the four profiles; with multiple entries the
    first that knows the person wins. An unknown person is a user error, so it
    raises :class:`ServiceValidationError` rather than a generic exception.
    """
    for coordinator in _loaded_coordinators(hass):
        if person_id in coordinator.data:
            return coordinator
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_person",
        translation_placeholders={"person_id": person_id},
    )


def _require_date(call: ServiceCall) -> date:
    """Return the ``date`` field, already parsed to a :class:`date` by cv.date."""
    value = call.data[ATTR_DATE]
    if not isinstance(value, date):  # pragma: no cover - cv.date guarantees this
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_date"
        )
    return value


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the §14 services once for this Home Assistant instance.

    Idempotent: if the services already exist (another entry registered them)
    nothing happens, so loading a second household entry does not raise.
    """
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        return

    async def _async_refresh(call: ServiceCall) -> None:
        """Limited manual recompute, optionally scoped to one person (spec §14)."""
        person_id = call.data.get(ATTR_PERSON_ID)
        if person_id is not None:
            coordinator = _find_profile(hass, person_id)
            await coordinator.async_request_refresh()
            return
        for coordinator in _loaded_coordinators(hass):
            await coordinator.async_request_refresh()

    async def _async_mark_departed(call: ServiceCall) -> None:
        """Close a mission's current plan after a confirmed departure (spec §10)."""
        person_id = call.data[ATTR_PERSON_ID]
        plan_id = call.data[ATTR_PLAN_ID]
        coordinator = _find_profile(hass, person_id)
        result = coordinator.data.get(person_id)
        plan = result.plan if result is not None else None
        if plan is None or plan.plan_id != plan_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_plan",
                translation_placeholders={"plan_id": plan_id},
            )
        store = coordinator._store
        state = store.get_mission(plan.mission_id)
        if state is None or state.status == "departed":
            return
        now = dt_util.utcnow()
        store.set_mission(
            replace(state, status="departed", departed_at=now, reopened=False)
        )
        await coordinator.async_request_refresh()

    async def _async_set_override(call: ServiceCall) -> None:
        """Write a per-day override (spec §14; arrival_time in local tz)."""
        person_id = call.data[ATTR_PERSON_ID]
        coordinator = _find_profile(hass, person_id)
        local_date = _require_date(call)
        mode: Mode | None = call.data.get(ATTR_MODE)
        attendance: Attendance | None = call.data.get(ATTR_ATTENDANCE)
        arrival_time: dt_time | None = call.data.get(ATTR_ARRIVAL_TIME)

        store = coordinator._store
        base = store.get_override(person_id, local_date) or DayOverride(
            person_id=person_id, local_date=local_date
        )
        # arrival_time is a wall-clock time for ``local_date`` in
        # Europe/Stockholm; it is stored as a ``time`` and only combined with
        # the local date when the plan is built (spec §14).
        override = replace(
            base,
            mode=mode if mode is not None else base.mode,
            attendance=attendance if attendance is not None else base.attendance,
            arrival_time=(
                arrival_time if arrival_time is not None else base.arrival_time
            ),
        )
        store.set_override(override)
        await coordinator.async_request_refresh()

    async def _async_clear_override(call: ServiceCall) -> None:
        """Drop a per-day override and recompute (spec §14)."""
        person_id = call.data[ATTR_PERSON_ID]
        coordinator = _find_profile(hass, person_id)
        local_date = _require_date(call)
        store = coordinator._store
        # Removing the key makes the day fall back to the profile defaults; the
        # debounced save is scheduled directly since there is no removed-override
        # accessor on the Store (parity with how the selects clear a value).
        if store.data.overrides.pop((person_id, local_date), None) is not None:
            store.async_schedule_save()
        await coordinator.async_request_refresh()

    async def _async_get_alternatives(call: ServiceCall) -> ServiceResponse:
        """Return the reachable journeys the current plan knows about (spec §14)."""
        person_id = call.data[ATTR_PERSON_ID]
        coordinator = _find_profile(hass, person_id)
        result = coordinator.data.get(person_id)
        plan = result.plan if result is not None else None
        return {"person_id": person_id, "alternatives": _alternatives_for(plan)}

    async def _async_select_journey(call: ServiceCall) -> None:
        """Record an explicitly chosen journey for the current plan (spec §14)."""
        person_id = call.data[ATTR_PERSON_ID]
        plan_id = call.data[ATTR_PLAN_ID]
        journey_id = call.data[ATTR_JOURNEY_ID]
        coordinator = _find_profile(hass, person_id)
        result = coordinator.data.get(person_id)
        plan = result.plan if result is not None else None
        if plan is None or plan.plan_id != plan_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_plan",
                translation_placeholders={"plan_id": plan_id},
            )
        store = coordinator._store
        stored = store.get_plan(plan.mission_id)
        if stored is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_plan",
                translation_placeholders={"plan_id": plan_id},
            )
        # Record the choice on the stored plan so the next recompute's
        # stickiness keeps it (planner prefers ``previous.journey_id``). No
        # immediate provider recompute is triggered here: that would re-derive
        # the journey from the planner and discard the user's verified choice.
        store.set_plan(replace(stored, journey_id=journey_id))

    async def _async_reopen_today(call: ServiceCall) -> None:
        """Explicitly reopen today's locked mission (spec §5.3, §14)."""
        person_id = call.data[ATTR_PERSON_ID]
        coordinator = _find_profile(hass, person_id)
        # Today's local date is computed in Europe/Stockholm, not HA's
        # configured timezone, so the mission id matches the coordinator's
        # (spec §14: local tz, not the server/UTC date).
        today = local_date_of(dt_util.utcnow())
        mission_id = make_mission_id(person_id, today)
        store = coordinator._store
        state = store.get_mission(mission_id)
        if state is None:
            return
        store.set_mission(replace(state, reopened=True))
        await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, _async_refresh, schema=_REFRESH_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MARK_DEPARTED,
        _async_mark_departed,
        schema=_MARK_DEPARTED_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_OVERRIDE, _async_set_override, schema=_SET_OVERRIDE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_OVERRIDE,
        _async_clear_override,
        schema=_CLEAR_OVERRIDE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_ALTERNATIVES,
        _async_get_alternatives,
        schema=_GET_ALTERNATIVES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SELECT_JOURNEY,
        _async_select_journey,
        schema=_SELECT_JOURNEY_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REOPEN_TODAY, _async_reopen_today, schema=_REOPEN_TODAY_SCHEMA
    )


@callback
def async_unload_services(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the §14 services when the last entry unloads (spec §15).

    Only remove once no *other* loaded entry remains, so unloading one
    household entry while another stays loaded keeps the services available.
    """
    if _loaded_coordinators(hass, exclude_entry_id=entry_id):
        return
    for service in _ALL_SERVICES:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


def _alternatives_for(plan: Any) -> list[dict[str, Any]]:
    """Build the response payload describing a plan's reachable journeys.

    The response is derived only from the published plan so the service makes
    no network calls: the recommended/selected journey always, plus the later
    on-time alternative when the plan found one (spec §6.2, §7). No raw API
    payloads, coordinates or URLs are exposed (review-privacy-security).
    """
    if plan is None or plan.recommended_leave is None:
        return []
    alternatives: list[dict[str, Any]] = [
        {
            "journey_id": plan.journey_id,
            "recommended_leave": _iso(plan.recommended_leave),
            "latest_leave": _iso(plan.latest_leave),
            "predicted_arrival": _iso(plan.predicted_arrival),
            "route_summary": plan.route_summary,
            "quality": plan.quality,
            "feasible": plan.feasible,
            "selected": True,
        }
    ]
    if plan.last_on_time_alternative_leave is not None:
        alternatives.append(
            {
                "journey_id": None,
                "recommended_leave": _iso(plan.last_on_time_alternative_leave),
                "latest_leave": _iso(plan.last_on_time_alternative_leave),
                "predicted_arrival": None,
                "route_summary": None,
                "quality": plan.quality,
                "feasible": True,
                "selected": False,
            }
        )
    return alternatives


def _iso(value: Any) -> str | None:
    """ISO-format an aware datetime for the response, or ``None``."""
    return value.isoformat() if value is not None else None


__all__ = ["async_setup_services", "async_unload_services"]
