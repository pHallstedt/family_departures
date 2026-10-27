"""The Family Departures integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DEFAULT_DRY_RUN, OPT_DRY_RUN, OPT_TEST_RECIPIENT_SCRIPT, PLATFORMS
from .coordinator import FamilyDeparturesCoordinator
from .dispatcher import NotificationDispatcher
from .scheduler import NotificationScheduler
from .services import async_setup_services, async_unload_services
from .store import FamilyDeparturesStore

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class FamilyDeparturesRuntimeData:
    """Per-entry runtime objects reachable from ``entry.runtime_data``.

    The entity platforms, services and diagnostics read the coordinator; the
    scheduler (timing + ledger) and dispatcher (delivery + companion actions)
    are kept here so they live and die with the entry.
    """

    coordinator: FamilyDeparturesCoordinator
    scheduler: NotificationScheduler
    dispatcher: NotificationDispatcher


type FamilyDeparturesConfigEntry = ConfigEntry[FamilyDeparturesRuntimeData]


async def async_setup_entry(
    hass: HomeAssistant, entry: FamilyDeparturesConfigEntry
) -> bool:
    """Set up Family Departures from a config entry.

    Builds the versioned Store and the per-entry coordinator, does a first
    refresh so entities have data on load, starts the notification dispatcher
    and scheduler, and forwards the entity platforms (spec §3, §11.3, §12.2).
    An options update listener reloads the entry so profile/settings changes
    made in the options flow take effect without leaving stale state (spec
    §4.1: options changes trigger a reload).

    Every teardown is registered with ``entry.async_on_unload`` *before* the
    matching start, so a failed setup still cleans up. HA runs those callbacks
    last-in-first-out: the scheduler stops first, then the dispatcher, and the
    store flush runs last so the notification ledger is on disk before a reload
    builds a new store (no resend after reload).
    """
    store = FamilyDeparturesStore(hass, entry.entry_id)
    await store.async_load()
    entry.async_on_unload(store.async_save)

    coordinator = FamilyDeparturesCoordinator(hass, entry, store)
    await coordinator.async_config_entry_first_refresh()

    def _dry_run() -> bool:
        return bool(entry.options.get(OPT_DRY_RUN, DEFAULT_DRY_RUN))

    def _test_recipient() -> str | None:
        return entry.options.get(OPT_TEST_RECIPIENT_SCRIPT) or None

    dispatcher = NotificationDispatcher(
        hass,
        store,
        lambda: coordinator.profile_configs,
        dry_run=_dry_run,
        test_recipient=_test_recipient,
        request_refresh=coordinator.async_request_refresh,
        aliases=coordinator.attendee_aliases,
    )
    entry.async_on_unload(dispatcher.async_shutdown)
    dispatcher.async_start()

    scheduler = NotificationScheduler(
        hass, coordinator, store, dispatcher.async_dispatch
    )
    entry.async_on_unload(scheduler.async_shutdown)
    await scheduler.async_start()

    entry.runtime_data = FamilyDeparturesRuntimeData(
        coordinator=coordinator, scheduler=scheduler, dispatcher=dispatcher
    )
    if _dry_run():
        _LOGGER.info(
            "Dry-run is on: notifications are logged and fired as "
            "family_departures_notification events, but no scripts are called. "
            "Turn it off under Configure → Settings"
        )

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

    The scheduler, dispatcher and store flush need no explicit calls here:
    their ``entry.async_on_unload`` callbacks run right after this returns.
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
    """Migrate an old config entry to the current schema (plan §3).

    Version 1 stored one source + destination per profile. Version 2 reshapes
    the entry into the household model: ``locations`` and ``calendars``
    registries plus profiles that reference them through calendar links. The
    migration is pure (``household.migrate_v1_to_v2``); the secret ICS URLs are
    re-keyed from profile id to calendar id in memory only, never logged.
    """
    from .household import migrate_v1_to_v2

    if entry.version > 2:
        # A downgrade: refuse rather than corrupt newer data.
        return False
    if entry.version == 1:
        new_data, new_options = migrate_v1_to_v2(entry.data, entry.options)
        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            options=new_options,
            version=2,
            minor_version=1,
        )
        _LOGGER.debug("Migrated config entry to version 2")
    return True
