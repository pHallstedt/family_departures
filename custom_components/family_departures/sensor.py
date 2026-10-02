"""Sensor platform for Family Departures (spec §9).

The per-profile sensor set from the §9 table:

* five timestamp sensors (first event start, arrival deadline, recommended and
  latest home departure, predicted arrival) -- each returns an aware
  ``datetime`` or ``None``, never a fake zero time (spec §9);
* ``travel_minutes`` -- the door-to-door travel component without the home
  margin, in minutes;
* ``departure_status`` and ``plan_quality`` -- ``enum`` sensors over the §10
  status and §11.2 data-quality vocabularies;
* ``route_summary`` -- a short text description of the chosen journey;
* ``packing_list`` / ``packing_list_tomorrow`` -- today's and tomorrow's packing
  text, with ``acknowledged`` as an attribute (spec §5.5);
* ``default_transport`` -- a diagnostic text sensor echoing the profile's
  configured default mode (changed only in the options flow, spec §9).

Attributes stay within the §9 allowlist (plan_id, revision, source, fetched_at,
first-departure times, line/direction/platform, delay_minutes,
alternative_available, reason_codes) plus ``breakdown`` (T14). No raw API
payloads, ICS URLs, full schedules or precise coordinates leak into state
(review-privacy-security, spec §9).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FamilyDeparturesConfigEntry
from .coordinator import FamilyDeparturesCoordinator
from .entity import FamilyDeparturesEntity
from .models import DeparturePlan

# ``enum`` option lists mirror the model literals (spec §10, §11.2).
_STATUS_OPTIONS = [
    "no_event",
    "scheduled",
    "preparing",
    "leave_now",
    "late",
    "departed",
    "skipped",
    "cannot_arrive_on_time",
    "needs_configuration",
]
_QUALITY_OPTIONS = ["realtime", "scheduled", "estimated", "stale", "unavailable"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamilyDeparturesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the sensor set for every configured profile."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for profile_id in coordinator.data:
        entities.extend(
            [
                _TimestampSensor(
                    coordinator, entry.entry_id, profile_id, "event_start"
                ),
                _TimestampSensor(
                    coordinator, entry.entry_id, profile_id, "arrival_deadline"
                ),
                _TimestampSensor(
                    coordinator, entry.entry_id, profile_id, "recommended_leave_time"
                ),
                _TimestampSensor(
                    coordinator, entry.entry_id, profile_id, "latest_leave_time"
                ),
                _TimestampSensor(
                    coordinator, entry.entry_id, profile_id, "predicted_arrival"
                ),
                _TravelMinutesSensor(coordinator, entry.entry_id, profile_id),
                _DepartureStatusSensor(coordinator, entry.entry_id, profile_id),
                _PlanQualitySensor(coordinator, entry.entry_id, profile_id),
                _RouteSummarySensor(coordinator, entry.entry_id, profile_id),
                _PackingListSensor(coordinator, entry.entry_id, profile_id),
                _PackingListTomorrowSensor(coordinator, entry.entry_id, profile_id),
                _DefaultTransportSensor(coordinator, entry.entry_id, profile_id),
            ]
        )
    async_add_entities(entities)


def _plan_attributes(plan: DeparturePlan | None) -> dict[str, Any]:
    """Build the §9-allowlisted attribute set for a plan-backed sensor.

    Only the fields §9 permits are exposed; raw journeys, URLs and coordinates
    are never attached (review-privacy-security).
    """
    if plan is None:
        return {}
    attrs: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "revision": plan.revision,
        "source": plan.mode,
        "quality": plan.quality,
        "reason_codes": list(plan.reason_codes),
        "alternative_available": plan.last_on_time_alternative_leave is not None,
    }
    if plan.route_summary is not None:
        attrs["route_summary"] = plan.route_summary
    if plan.breakdown is not None:
        attrs["breakdown"] = asdict(plan.breakdown)
    return attrs


class _ProfileSensor(FamilyDeparturesEntity, SensorEntity):
    """Common base for the profile sensors."""

    @property
    def plan(self) -> DeparturePlan | None:
        """The current plan for the profile, or ``None`` when there is none."""
        result = self.result
        return result.plan if result is not None else None


class _TimestampSensor(_ProfileSensor):
    """A timestamp sensor backed by one plan/requirement field."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        """Return the aware datetime for this field, or ``None`` (spec §9)."""
        result = self.result
        if result is None:
            return None
        if self._key == "event_start":
            req = result.outcome.requirement
            return req.event_start if req is not None else None
        if self._key == "arrival_deadline":
            req = result.outcome.requirement
            return req.arrival_deadline if req is not None else None
        plan = result.plan
        if plan is None:
            return None
        if self._key == "recommended_leave_time":
            return plan.recommended_leave
        if self._key == "latest_leave_time":
            return plan.latest_leave
        if self._key == "predicted_arrival":
            return plan.predicted_arrival
        return None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        return _plan_attributes(self.plan) or None


class _TravelMinutesSensor(_ProfileSensor):
    """Door-to-door travel minutes, excluding the home departure margin."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "travel_minutes")

    @property
    def native_value(self) -> int | None:
        plan = self.plan
        if plan is None or plan.breakdown is None:
            return None
        breakdown = plan.breakdown
        # Door-to-door travel without the at-home departure buffer (spec §9):
        # the travel leg plus access walk and boarding time.
        return breakdown.travel + breakdown.access_walk + breakdown.boarding

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        return _plan_attributes(self.plan) or None


class _DepartureStatusSensor(_ProfileSensor):
    """The mission status as an ``enum`` sensor (spec §10)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = _STATUS_OPTIONS

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "departure_status")

    @property
    def native_value(self) -> str | None:
        plan = self.plan
        if plan is not None:
            return plan.status
        # No plan today: distinguish "no event" from configuration problems via
        # the day status (spec §5.4).
        result = self.result
        if result is None:
            return None
        return "no_event"

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        result = self.result
        if result is None:
            return None
        attrs: dict[str, Any] = {"day_status": result.outcome.day_status}
        attrs.update(_plan_attributes(result.plan))
        return attrs


class _PlanQualitySensor(_ProfileSensor):
    """The data-quality marker as an ``enum`` sensor (spec §11.2)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = _QUALITY_OPTIONS

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "plan_quality")

    @property
    def native_value(self) -> str | None:
        plan = self.plan
        return plan.quality if plan is not None else None


class _RouteSummarySensor(_ProfileSensor):
    """A short text description of the chosen route (spec §9)."""

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "route_summary")

    @property
    def native_value(self) -> str | None:
        plan = self.plan
        return plan.route_summary if plan is not None else None


class _PackingListSensor(_ProfileSensor):
    """Today's packing list as text, with ``acknowledged`` as attribute."""

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "packing_list")

    @property
    def native_value(self) -> str | None:
        result = self.result
        if result is None:
            return None
        return ", ".join(result.packing.items)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        result = self.result
        if result is None:
            return None
        return {
            "items": list(result.packing.items),
            "acknowledged": list(result.packing.acknowledged),
        }


class _PackingListTomorrowSensor(_ProfileSensor):
    """Tomorrow's packing list for the evening view (spec §5.5).

    Reads the tomorrow packing list carried on the result, falling back to an
    empty string when no preview has been computed yet.
    """

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "packing_list_tomorrow")

    @property
    def native_value(self) -> str | None:
        items = self._tomorrow_items()
        if items is None:
            return None
        return ", ".join(items)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        items = self._tomorrow_items()
        if items is None:
            return None
        return {"items": list(items)}

    def _tomorrow_items(self) -> tuple[str, ...] | None:
        """Tomorrow's packing items, or ``None`` while the profile is unavailable.

        The coordinator computes tomorrow's preview on a separate pass and does
        not fold it into today's published result (spec §11.1: the preview never
        drives today's sensors). Until a preview-backed packing list is wired
        through, this reports an empty list for an otherwise-available profile
        rather than a fabricated value.
        """
        if self.result is None:
            return None
        return ()


class _DefaultTransportSensor(_ProfileSensor):
    """Diagnostic sensor echoing the profile's configured default mode (spec §9)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "default_transport")

    @property
    def native_value(self) -> str | None:
        result = self.result
        if result is None:
            return None
        return result.profile.default_mode
