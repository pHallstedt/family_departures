"""Config-entry diagnostics for Family Departures (spec §15, T19).

The export has to be safe to paste into an issue tracker: it must never leak the
secret ICS URLs, the household/destination coordinates, the family members'
names, or any schedule content (event summaries/descriptions). Everything that
could identify a person or a place is run through
:func:`homeassistant.components.diagnostics.async_redact_data` before it leaves
the process (review-privacy-security, spec §15).

What is kept is operational: the integration version, per-profile mode/source
*type* (not the URL), the current plan *status*/quality/timestamps, and the
per-source health counters (last success, last error code, cache hits, external
call counts). Those are the figures §15 says the diagnostic sensors/logs may
show.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import FamilyDeparturesConfigEntry
from .const import DATA_HOME_LAT, DATA_HOME_LON, DATA_ICS_URLS
from .coordinator import CoordinatorData, ProfileResult

# Keys whose *values* must never appear in a diagnostics dump. The ICS URLs,
# coordinates, display names and event text are all identifying (spec §15).
TO_REDACT: set[str] = {
    DATA_ICS_URLS,
    DATA_HOME_LAT,
    DATA_HOME_LON,
    "home_lat",
    "home_lon",
    "dest_lat",
    "dest_lon",
    "latitude",
    "longitude",
    "name",
    "household_name",
    "calendar_entity_id",
    "person_entity_id",
    "summary",
    "description",
    "static_label",
    "route_summary",
    "ics_url",
    "url",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _plan_diagnostics(result: ProfileResult) -> dict[str, Any] | None:
    """Operational view of a profile's current plan (no route text/coords)."""
    plan = result.plan
    if plan is None:
        return None
    return {
        "status": plan.status,
        "mode": plan.mode,
        "quality": plan.quality,
        "feasible": plan.feasible,
        "revision": plan.revision,
        "config_revision": plan.config_revision,
        "recommended_leave": _iso(plan.recommended_leave),
        "latest_leave": _iso(plan.latest_leave),
        "predicted_arrival": _iso(plan.predicted_arrival),
        "reason_codes": list(plan.reason_codes),
    }


def _profile_diagnostics(result: ProfileResult) -> dict[str, Any]:
    """Per-profile operational snapshot.

    Only counts and statuses are included for the schedule/packing - never the
    event summaries or the packing-item *text*, which could reveal a child's
    timetable (spec §15 redaction). The display name is included under ``name``
    so :data:`TO_REDACT` scrubs it rather than silently dropping the field.
    """
    config = result.profile
    return {
        "id": config.id,
        "name": config.name,
        "source_type": config.source_type,
        "default_mode": config.default_mode,
        "weekday_mask": sorted(config.weekday_mask),
        "notifications_enabled": config.notifications_enabled,
        "evening_notice_enabled": config.evening_notice_enabled,
        "day_status": result.outcome.day_status,
        "has_requirement": result.outcome.requirement is not None,
        "reason_codes": list(result.outcome.reason_codes),
        "schedule_status": result.schedule.status,
        "schedule_stale": result.schedule.stale,
        "schedule_error_code": result.schedule.error_code,
        "schedule_event_count": len(result.schedule.events),
        "packing_item_count": len(result.packing.items),
        "packing_acknowledged_count": len(result.packing.acknowledged),
        "plan": _plan_diagnostics(result),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FamilyDeparturesConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for one config entry (spec §15)."""
    coordinator = entry.runtime_data

    data: CoordinatorData = coordinator.data or {}
    profiles = [_profile_diagnostics(result) for result in data.values()]

    source_health = {
        destination_id: health.as_diagnostics()
        for destination_id, health in coordinator.source_health().items()
    }

    diagnostics: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            # entry.data and entry.options carry URLs/coords/names; redact them.
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "config_revision": coordinator.config_revision,
        "profile_count": len(coordinator.source_health()),
        "profiles": profiles,
        "source_health": source_health,
        "invalid_sources": coordinator.invalid_sources,
    }
    return async_redact_data(diagnostics, TO_REDACT)
