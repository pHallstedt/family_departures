"""Switch platform for Family Departures (spec §9).

One per-profile switch, ``departure_notifications``, turning this profile's
departure notifications on or off. Per §4.1 the notification switch is the one
persistent setting an entity may change; it writes the profile's
``notifications_enabled`` flag back into ``entry.options`` and lets the options
update listener reload the entry (review-ha-integration, spec §4.1). The secret
ICS URL lives in ``entry.data`` and is never touched here
(review-privacy-security).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import FamilyDeparturesConfigEntry
from .const import OPT_PROFILES
from .coordinator import FamilyDeparturesCoordinator
from .entity import FamilyDeparturesEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamilyDeparturesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the notification switch for every configured profile."""
    coordinator = entry.runtime_data
    async_add_entities(
        _NotificationsSwitch(coordinator, entry, profile_id)
        for profile_id in coordinator.data
    )


class _NotificationsSwitch(FamilyDeparturesEntity, SwitchEntity):
    """Enables or disables departure notifications for one profile (spec §9)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry: FamilyDeparturesConfigEntry,
        profile_id: str,
    ) -> None:
        super().__init__(
            coordinator, entry.entry_id, profile_id, "departure_notifications"
        )
        self._entry = entry

    @property
    def is_on(self) -> bool | None:
        result = self.result
        if result is None:
            return None
        return result.profile.notifications_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_enabled(False)

    async def _set_enabled(self, enabled: bool) -> None:
        """Write ``notifications_enabled`` to options; the listener reloads.

        The write is a no-op when the stored value already matches, so toggling
        to the current state does not trigger a reload (spec §4.1).
        """
        profiles = self._entry.options.get(OPT_PROFILES, {}) or {}
        current = profiles.get(self._profile_id)
        if current is None:
            return
        if bool(current.get("notifications_enabled", False)) == enabled:
            return
        new_options = deepcopy(dict(self._entry.options))
        new_profiles = dict(new_options.get(OPT_PROFILES, {}))
        new_profile = dict(new_profiles[self._profile_id])
        new_profile["notifications_enabled"] = enabled
        new_profiles[self._profile_id] = new_profile
        new_options[OPT_PROFILES] = new_profiles
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
