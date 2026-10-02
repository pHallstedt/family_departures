"""The Family Departures integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .coordinator import FamilyDeparturesCoordinator
from .services import async_setup_services, async_unload_services
from .store import FamilyDeparturesStore

# The coordinator is attached to the entry's ``runtime_data`` so the entity
# platforms (T14) can reach it without a global ``hass.data`` lookup.
type FamilyDeparturesConfigEntry = ConfigEntry[FamilyDeparturesCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: FamilyDeparturesConfigEntry
) -> bool:
    """Set up Family Departures from a config entry.

    Builds the versioned Store and the per-entry coordinator, does a first
    refresh so entities have data on load, and forwards the entity platforms
    (spec §3). An options update listener reloads the entry so profile/settings
    changes made in the options flow take effect without leaving stale state
    (spec §4.1: options changes trigger a reload).
    """
    store = FamilyDeparturesStore(hass, entry.entry_id)
    await store.async_load()

    coordinator = FamilyDeparturesCoordinator(hass, entry, store)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the §14 household actions once; they are shared across entries
    # and removed again when the last entry unloads (spec §14, §15).
    async_setup_services(hass)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: FamilyDeparturesConfigEntry
) -> None:
    """Reload the entry when its options change (spec §4.1)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: FamilyDeparturesConfigEntry
) -> bool:
    """Unload a config entry, cleaning up any platforms it set up.

    The §14 services are removed only once the last entry has unloaded so that
    unloading one household entry does not strip the actions from another
    (spec §15).
    """
    if PLATFORMS:
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    else:
        unloaded = True
    if unloaded:
        async_unload_services(hass, entry.entry_id)
    return unloaded


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry.

    Version 1 is the first released schema, so there is nothing to migrate yet.
    The hook exists so future schema changes have a home.
    """
    return True
