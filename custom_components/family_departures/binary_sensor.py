"""Binary sensor platform for Family Departures (spec §9).

Two per-profile booleans from the §9 table:

* ``departure_disruption`` -- a known impact on today's journey: the plan is
  infeasible / late / cannot-arrive, or carries a disruption reason code (spec
  §9, §11.2);
* ``schedule_needs_review`` -- the schedule source, filter or freshness needs a
  look: the fetch errored or served a stale cache, or the day could not be
  classified (spec §5.4, §11.2), an unknown tag or deleted location fell back
  to a default, an extra calendar failed, or an activity is back-to-back with or
  overlaps another commitment (``reasons`` attribute, codes only).

Both are ``PROBLEM`` device-class sensors so the dashboard can surface them.
No raw source data is exposed in attributes (review-privacy-security).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FamilyDeparturesConfigEntry
from .coordinator import FamilyDeparturesCoordinator, ProfileResult
from .entity import FamilyDeparturesEntity
from .missions import (
    REASON_EXTRA_SOURCE_ERROR,
    REASON_UNKNOWN_LINK_LOCATION,
    REASON_UNKNOWN_LOCATION_TAG,
    REASON_UNKNOWN_TRANSPORT_TAG,
)

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

# Reason codes that mean a calendar entry, tag or extra link needs a look
# (plan §5.3, §4.3); together with mission warnings they turn the sensor on.
_REVIEW_REASON_CODES = frozenset(
    {
        REASON_EXTRA_SOURCE_ERROR,
        REASON_UNKNOWN_LINK_LOCATION,
        REASON_UNKNOWN_LOCATION_TAG,
        REASON_UNKNOWN_TRANSPORT_TAG,
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamilyDeparturesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the binary sensors for every configured profile."""
    coordinator = entry.runtime_data.coordinator
    entities: list[BinarySensorEntity] = []
    for profile_id in coordinator.profile_configs:
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
        return bool(self._reasons(result))

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        result = self.result
        if result is None:
            return None
        return {"reasons": self._reasons(result)}

    @staticmethod
    def _reasons(result: ProfileResult) -> list[str]:
        """Why the schedule needs a look (codes only, never event text)."""
        reasons: list[str] = []
        schedule = result.schedule
        if schedule.status == "error" or schedule.stale:
            reasons.append("source_error")
        # The source could not be classified into a usable day (spec §5.4).
        elif result.outcome.day_status in ("no_schedule", "source_error"):
            reasons.append(result.outcome.day_status)
        reasons.extend(
            code
            for code in result.outcome.reason_codes
            if code in _REVIEW_REASON_CODES and code not in reasons
        )
        for mission in result.missions:
            reasons.extend(
                warning
                for warning in mission.requirement.warnings
                if warning not in reasons
            )
            reasons.extend(
                code
                for code in mission.plan.reason_codes
                if code in _REVIEW_REASON_CODES and code not in reasons
            )
        return reasons
