"""Shared entity base for the Family Departures integration (spec §9).

Every entity belongs to exactly one profile and to that profile's device, so
the dashboard groups per family member. Unique IDs are derived from the config
entry id, the stable profile id and an entity key -- never from the display
name, which the user may change (review-ha-integration, spec §9).

The base is a :class:`CoordinatorEntity` over
:class:`FamilyDeparturesCoordinator`, whose ``data`` maps profile id ->
:class:`ProfileResult`. Subclasses read the current result through
:meth:`result` and must tolerate ``None`` (a profile that failed to update or
was dropped mid-reconfigure, spec §15).
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import FamilyDeparturesCoordinator, ProfileResult


class FamilyDeparturesEntity(CoordinatorEntity[FamilyDeparturesCoordinator]):
    """Base entity bound to one profile and its device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._profile_id = profile_id
        self._key = key
        # Stable unique id: entry + profile + key, independent of display name
        # (spec §9, review-ha-integration).
        self._attr_unique_id = f"{entry_id}_{profile_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{profile_id}")},
            name=self._profile_name,
            manufacturer="Family Departures",
            model="Departure profile",
        )

    @property
    def _profile_name(self) -> str:
        """The profile's current display name, falling back to its id."""
        result = self.result
        if result is not None:
            return result.profile.name
        return self._profile_id

    @property
    def result(self) -> ProfileResult | None:
        """The latest per-profile result, or ``None`` if unavailable."""
        data = self.coordinator.data
        if not data:
            return None
        return data.get(self._profile_id)

    @property
    def available(self) -> bool:
        """A profile is available while the coordinator holds a result for it.

        A profile that failed its update round (dropped from the data map, spec
        §15) goes unavailable without taking the others down.
        """
        return super().available and self.result is not None
