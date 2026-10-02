"""Button platform for Family Departures (spec §9).

Three per-profile buttons from the §9 table:

* ``ack_packing`` -- acknowledge today's packing list. Tomorrow's list is
  acknowledged through the evening notice, not here (spec §5.5, §9).
* ``refresh_departure`` -- a limited manual recompute (spec §9).
* ``mark_departed`` -- confirm departure for today's mission, which closes it so
  no further leave/critical pushes go out that day (spec §10).

The manual buttons work without Companion/presence (spec §9).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import FamilyDeparturesConfigEntry
from .coordinator import FamilyDeparturesCoordinator
from .entity import FamilyDeparturesEntity
from .timeutil import local_date_of, make_mission_id


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamilyDeparturesConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the action buttons for every configured profile."""
    coordinator = entry.runtime_data
    entities: list[ButtonEntity] = []
    for profile_id in coordinator.data:
        entities.append(_AckPackingButton(coordinator, entry.entry_id, profile_id))
        entities.append(_RefreshButton(coordinator, entry.entry_id, profile_id))
        entities.append(_MarkDepartedButton(coordinator, entry.entry_id, profile_id))
    async_add_entities(entities)


class _ProfileButton(FamilyDeparturesEntity, ButtonEntity):
    """Common base for the profile buttons."""

    def _today(self) -> date:
        return local_date_of(dt_util.utcnow())


class _AckPackingButton(_ProfileButton):
    """Acknowledge today's packing list (spec §5.5, §9)."""

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "ack_packing")

    async def async_press(self) -> None:
        result = self.result
        if result is None:
            return
        today = self._today()
        store = self.coordinator._store
        # Union of already-acknowledged and the current items, so pressing twice
        # or after a new item appeared keeps every acknowledgement (spec §5.5).
        existing = set(store.get_packing_acks(self._profile_id, today))
        existing.update(result.packing.items)
        store.set_packing_acks(self._profile_id, today, tuple(sorted(existing)))
        await self.coordinator.async_request_refresh()


class _RefreshButton(_ProfileButton):
    """A limited manual recompute (spec §9)."""

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "refresh_departure")

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class _MarkDepartedButton(_ProfileButton):
    """Confirm departure for today's mission (spec §10)."""

    def __init__(
        self,
        coordinator: FamilyDeparturesCoordinator,
        entry_id: str,
        profile_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id, profile_id, "mark_departed")

    async def async_press(self) -> None:
        result = self.result
        if result is None or result.plan is None:
            return
        now = dt_util.utcnow()
        mission_id = make_mission_id(self._profile_id, self._today())
        store = self.coordinator._store
        state = store.get_mission(mission_id)
        if state is None:
            # No mission state yet (e.g. button pressed before the first plan
            # persisted): nothing to confirm against.
            return
        if state.status == "departed":
            return
        store.set_mission(
            replace(state, status="departed", departed_at=now, reopened=False)
        )
        await self.coordinator.async_request_refresh()
