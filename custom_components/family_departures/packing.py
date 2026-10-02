"""Packing-list building from a day's schedule (spec §5.5).

:func:`build_packing_list` turns a day's included events into a per-person list
of things to bring, driven by the profile's :class:`PackingRule` list. A rule is
a case-insensitive substring that is matched against the ``SUMMARY`` of *every*
included event of the local day (not just the first), so an afternoon PE lesson
still yields a morning reminder (§5.5).

This module is pure (plan §1 rule 3): no ``homeassistant`` import and no
``now`` dependency. It never influences departure times and never creates a
mission of its own.

Semantics (spec §5.5):

* The result is a day list of unique items; several matching lessons the same
  day collapse to one line, and the first-matched order is preserved.
* An ``off``/``sick``/``remote`` day override empties the list. A lesson that
  was cancelled or removed since the last fetch simply stops matching, so its
  line disappears on the next build.
* Acknowledgement is per ``(person_id, local_date)`` and covers only the items
  that existed when the "Packat" button was pressed. ``items`` therefore holds
  the items still *pending* (matched today minus acknowledged); a line added
  after the acknowledgement is not in ``acknowledged`` and so shows again.
  ``acknowledged`` is kept but trimmed to the items that still match today, so a
  removed lesson drops its stale acknowledgement too.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from .models import (
    DayOverride,
    PackingList,
    PackingRule,
    ScheduleEvent,
)

# Day overrides that mean the person is not attending, so nothing to pack.
_CLOSING_ATTENDANCE = frozenset(("off", "sick", "remote"))


def _rule_matches(summary: str, match: str) -> bool:
    """Case-insensitive substring match, consistent with source filtering."""
    return match.casefold() in summary.casefold()


def _matched_items(
    events: Iterable[ScheduleEvent], rules: Iterable[PackingRule]
) -> tuple[str, ...]:
    """Unique items for the day, in first-match order."""
    items: list[str] = []
    seen: set[str] = set()
    rule_list = tuple(rules)
    for event in events:
        for rule in rule_list:
            if rule.item in seen:
                continue
            if _rule_matches(event.summary, rule.match):
                seen.add(rule.item)
                items.append(rule.item)
    return tuple(items)


def build_packing_list(
    person_id: str,
    local_date: date,
    events: tuple[ScheduleEvent, ...],
    rules: tuple[PackingRule, ...],
    override: DayOverride | None,
    acknowledged: tuple[str, ...],
) -> PackingList:
    """Build the packing list for ``person_id`` on ``local_date``.

    ``events`` are the day's *included* events (already filtered by the schedule
    source). ``rules`` are the profile's packing rules. ``override`` closes the
    list when attendance is ``off``/``sick``/``remote``. ``acknowledged`` is the
    set of items the person has already ticked off for this date.

    Returns a :class:`PackingList` whose ``items`` are the still-pending items
    and whose ``acknowledged`` is the acknowledgement set trimmed to the items
    that still match today.
    """
    if override is not None and override.attendance in _CLOSING_ATTENDANCE:
        return PackingList(
            person_id=person_id,
            local_date=local_date,
            items=(),
            acknowledged=(),
        )

    matched = _matched_items(events, rules)
    acked_today = tuple(item for item in matched if item in acknowledged)
    pending = tuple(item for item in matched if item not in acknowledged)

    return PackingList(
        person_id=person_id,
        local_date=local_date,
        items=pending,
        acknowledged=acked_today,
    )
