"""Repairs for Family Departures (spec §15, T19).

The issues themselves - invalid source, missing destination and repeated fetch
failures - are *created and cleared* by the coordinator (see
``coordinator._reconcile_issues``), because that is where the per-source health
and the profile build results live. They are informational
(``is_fixable=False``): the household fixes them in the integration's options,
not through a guided repair flow, so no custom step is needed.

This module exists so Home Assistant has a repair-flow factory to call if an
issue is ever registered as fixable. It hands back the built-in confirm flow,
which simply acknowledges and dismisses the issue.
"""

from __future__ import annotations

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Return a repair flow for ``issue_id``.

    All of this integration's issues are non-fixable, so the default confirm
    flow is sufficient; it is only reached if an issue is later marked fixable.
    """
    return ConfirmRepairFlow()
