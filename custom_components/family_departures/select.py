"""Select platform for Family Departures (spec §9).

Two per-profile day controls that write a :class:`DayOverride` for *today* and
request a refresh (spec §9: day controls apply to the local date and reset the
next day):

* ``today_transport`` -- ``default`` / ``public_transport`` / ``car`` /
  ``static``. ``default`` clears the day's mode override so the profile falls
  back to its configured default (spec §4.3).
* ``today_attendance`` -- ``normal`` / ``off`` / ``remote`` / ``sick``.
  ``normal`` clears the day's attendance override.

These are the only persistent-looking writes an entity may make, and they go to
the Store's per-day overrides, never to the profile's persistent config
(review-ha-integration, spec §4.1). Changing a value writes the override and
asks the coordinator to recompute, so a stale plan's timers cannot restore the
old advice (spec §11.3).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import cast

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import FamilyDeparturesConfigEntry
from .coordinator import FamilyDeparturesCoordinator
from .entity import FamilyDeparturesEntity
from .models import Attendance, DayOverride, Mode
from .timeutil import local_date_of

_TRANSPORT_OPTIONS = ["default", "public_transport", "car", "static"]
_ATTENDANCE_OPTIONS = ["normal", "off", "remote", "sick"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamilyDeparturesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the day-control selects for every configured profile."""
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = []
    for profile_id in coordinator.data:
        entities.append(_TransportSelect(coordinator, entry.entry_id, profile_id))
        entities.append(_AttendanceSelect(coordinator, entry.entry_id, profile_id))
    async_add_entities(entities)


class _DayOverrideSelect(FamilyDeparturesEntity, SelectEntity):
    """Base for the day-override selects."""

    def _today(self) -> date:
        """Today's local date (the override's key, spec §9)."""
        return local_date_of(dt_util.utcnow())

    def _current_override(self) -> DayOverride | None:
        return self.coordinator._store.get_override(self._profile_id, self._today())

    async def _write_override(self, override: DayOverride) -> None:
        """Persist today's override and recompute (spec §11.3).

        A "cleared" override keeps all fields ``None``; the coordinator and
        schedule selection treat an all-``None`` override as no override, so the
        day falls back to the profile defaults (spec §4.3) without needing a
        separate delete path.
        """
        self.coordinator._store.set_override(override)
        await self.coordinator.async_request_refresh()


class _TransportSelect(_DayOverrideSelect):
    """Today's travel mode override (spec §9)."""

    _attr_options = _TRANSPORT_OPTIONS

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "today_transport")

    @property
    def current_option(self) -> str | None:
        override = self._current_override()
        if override is None or override.mode is None:
            return "default"
        return override.mode

    async def async_select_option(self, option: str) -> None:
        base = self._current_override() or DayOverride(
            person_id=self._profile_id, local_date=self._today()
        )
        # "default" clears the mode override (field set to None); the day then
        # uses the profile's configured default mode (spec §4.3).
        mode = None if option == "default" else cast(Mode, option)
        await self._write_override(replace(base, mode=mode))


class _AttendanceSelect(_DayOverrideSelect):
    """Today's attendance override (spec §9)."""

    _attr_options = _ATTENDANCE_OPTIONS

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "today_attendance")

    @property
    def current_option(self) -> str | None:
        override = self._current_override()
        if override is None or override.attendance is None:
            return "normal"
        return override.attendance

    async def async_select_option(self, option: str) -> None:
        base = self._current_override() or DayOverride(
            person_id=self._profile_id, local_date=self._today()
        )
        # "normal" clears the attendance override; the day runs as scheduled.
        attendance = None if option == "normal" else cast(Attendance, option)
        await self._write_override(replace(base, attendance=attendance))
