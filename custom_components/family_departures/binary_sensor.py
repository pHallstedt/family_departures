"""Binary sensor platform for Family Departures (spec §9).

Two per-profile booleans from the §9 table:

* ``departure_disruption`` -- a known impact on today's journey: the plan is
  infeasible / late / cannot-arrive, or carries a disruption reason code (spec
  §9, §11.2);
* ``schedule_needs_review`` -- the schedule source, filter or freshness needs a
  look: the fetch errored or served a stale cache, or the day could not be
  classified (spec §5.4, §11.2).

Both are ``PROBLEM`` device-class sensors so the dashboard can surface them.
No raw source data is exposed in attributes (review-privacy-security).
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FamilyDeparturesConfigEntry
from .coordinator import FamilyDeparturesCoordinator
from .entity import FamilyDeparturesEntity

# Reason codes that signal an active disruption to the journey (spec §11.2).
_DISRUPTION_REASON_CODES = frozenset(
    {
        "cancelled",
        "cancelled_leg",
        "disruption",
        "delayed",
        "missed_transfer",
        "no_journey",
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamilyDeparturesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the binary sensors for every configured profile."""
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for profile_id in coordinator.data:
        entities.append(
            _DisruptionBinarySensor(coordinator, entry.entry_id, profile_id)
        )
        entities.append(
            _NeedsReviewBinarySensor(coordinator, entry.entry_id, profile_id)
        )
    async_add_entities(entities)


class _DisruptionBinarySensor(FamilyDeparturesEntity, BinarySensorEntity):
    """True when today's journey is known to be impacted (spec §9, §11.2)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "departure_disruption")

    @property
    def is_on(self) -> bool | None:
        result = self.result
        if result is None:
            return None
        plan = result.plan
        if plan is None:
            return False
        if plan.status in ("late", "cannot_arrive_on_time"):
            return True
        if not plan.feasible:
            return True
        return bool(_DISRUPTION_REASON_CODES.intersection(plan.reason_codes))


class _NeedsReviewBinarySensor(FamilyDeparturesEntity, BinarySensorEntity):
    """True when the schedule source/filter/freshness needs checking (spec §5.4)."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "schedule_needs_review")

    @property
    def is_on(self) -> bool | None:
        result = self.result
        if result is None:
            return None
        schedule = result.schedule
        if schedule.status == "error" or schedule.stale:
            return True
        # The source could not be classified into a usable day (spec §5.4).
        return result.outcome.day_status in ("no_schedule", "source_error")
