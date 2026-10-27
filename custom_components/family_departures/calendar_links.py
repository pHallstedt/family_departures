"""Per-link event filtering for shared calendars (plan §4.1, §4.5, F1/F5).

In v2 a calendar is a household resource fetched once per ``(calendar_id, day)``
and shared by every profile that links it (D1). Each profile derives its own
view of a calendar day by applying its :class:`CalendarLink`'s filters:

* the link's include/exclude *summary* patterns (same case-insensitive
  substring semantics as ``providers.ics._matches_any``), and
* the link's ``attendee_mode`` against the ``vem:``/``who:`` attendee
  tags parsed into :attr:`EventTags.attendees` (plan §4.5).

:func:`filter_for_link` is pure; attendee matching uses a pre-resolved set of
*aliases* for the linking profile (its display name, the HA person's friendly
name and the person object id, all casefolded). Resolving those aliases needs
``hass.states`` and therefore lives in the coordinator, but the matching itself
stays here so it is unit-testable without Home Assistant.

Shared-calendar privacy (plan §10, review-privacy-security): the aliases contain
person names, so this module never logs them; it only compares casefolded
strings in memory.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from datetime import date
from typing import TYPE_CHECKING

from .event_tags import resolve_mode_alias
from .models import CalendarLink, LinkRule, Location, ProfileConfig, ScheduleEvent

if TYPE_CHECKING:
    from .store import FamilyDeparturesStore


def _matches_any(summary: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive substring match (mirrors ``providers.ics._matches_any``)."""
    folded = summary.casefold()
    return any(pattern.casefold() in folded for pattern in patterns)


def _attendee_match(event: ScheduleEvent, aliases: Set[str]) -> bool:
    """Whether an event's attendee tags name this profile's person.

    ``aliases`` is the casefolded set of the profile's aliases. An event names
    the person when any of its casefolded attendee tags is in ``aliases``.
    """
    if not event.tags.attendees:
        return False
    return any(attendee.casefold() in aliases for attendee in event.tags.attendees)


def _passes_attendee_filter(
    event: ScheduleEvent, link: CalendarLink, aliases: Set[str]
) -> bool:
    """Apply ``link.attendee_mode`` to one event (plan §4.5).

    * ``all``: every event applies regardless of attendee tags.
    * ``tagged``: only events whose attendee tags name this person.
    * ``tagged_or_untagged``: untagged events apply to everyone; tagged events
      apply only to the named persons.
    """
    if link.attendee_mode == "all":
        return True
    tagged = bool(event.tags.attendees)
    if link.attendee_mode == "tagged":
        return tagged and _attendee_match(event, aliases)
    # tagged_or_untagged
    if not tagged:
        return True
    return _attendee_match(event, aliases)


def filter_for_link(
    events: tuple[ScheduleEvent, ...],
    link: CalendarLink,
    attendee_aliases: Set[str] = frozenset(),
) -> tuple[ScheduleEvent, ...]:
    """Return the events of one calendar day that apply to ``link``.

    Applies the link's include/exclude summary patterns and then its
    ``attendee_mode`` against ``attendee_aliases`` (the linking profile's
    casefolded aliases). Order is preserved. ``attendee_aliases`` defaults to
    the empty set, which is correct for a link whose mode is ``all``; a
    ``tagged``/``tagged_or_untagged`` link with no aliases then matches only
    untagged events, never leaking another person's tagged events.
    """
    exclude = link.source_filter.exclude_patterns
    include = link.source_filter.include_patterns
    kept: list[ScheduleEvent] = []
    for event in events:
        summary = event.summary or ""
        if exclude and _matches_any(summary, exclude):
            continue
        if include and not _matches_any(summary, include):
            continue
        if not _passes_attendee_filter(event, link, attendee_aliases):
            continue
        kept.append(event)
    return tuple(kept)


def profile_day_events(
    store: FamilyDeparturesStore,
    profile: ProfileConfig,
    d: date,
    aliases: Set[str] = frozenset(),
) -> tuple[ScheduleEvent, ...]:
    """Return the profile's events for ``d`` across all of its calendar links.

    Reads the cached calendar days (keyed by calendar id) from the store and
    applies each link's filter, including attendee matching against ``aliases``.
    Events are returned in start order. A calendar with no cached day
    contributes nothing.
    """
    collected: list[ScheduleEvent] = []
    for link in profile.calendar_links:
        schedule = store.get_schedule(link.calendar_id, d)
        if schedule is None:
            continue
        collected.extend(filter_for_link(schedule.events, link, aliases))
    return tuple(sorted(collected, key=lambda e: e.start))


RULE_SEPARATOR = "=>"


class RulesError(ValueError):
    """A rules text line that cannot be parsed or resolved (form error code)."""


def _resolve_location(token: str, locations: Mapping[str, Location]) -> str | None:
    """Casefolded exact match of ``token`` on a location id, name or alias."""
    folded = token.casefold()
    for location in locations.values():
        if location.id.casefold() == folded:
            return location.id
    for location in locations.values():
        if any(
            name.casefold() == folded for name in (location.name, *location.aliases)
        ):
            return location.id
    return None


def parse_rules_text(
    text: str, locations: Mapping[str, Location]
) -> tuple[LinkRule, ...]:
    """Parse the per-link summary rules typed in the options flow (plan §5.2).

    One rule per line, ``<summary match> => <token>[, <token>]``, for example
    ``IDRO => Sporthallen`` or ``Simning => Badhuset, kollektivt``. Each token is
    resolved as a transport-mode alias first, then as a location id, name or
    alias. A rule may set one mode and one location. Blank lines are skipped.
    Raises :class:`RulesError` for a line without ``=>``, an empty match, no
    tokens, an unknown token, or two tokens of the same kind.
    """
    rules: list[LinkRule] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match, separator, rest = line.partition(RULE_SEPARATOR)
        match = match.strip()
        tokens = [token.strip() for token in rest.split(",") if token.strip()]
        if not separator or not match or not tokens:
            raise RulesError("rules_invalid")
        location_id: str | None = None
        mode = None
        for token in tokens:
            resolved_mode = resolve_mode_alias(token)
            if resolved_mode is not None:
                if mode is not None:
                    raise RulesError("rules_invalid")
                mode = resolved_mode
                continue
            resolved_location = _resolve_location(token, locations)
            if resolved_location is None or location_id is not None:
                raise RulesError("rules_invalid")
            location_id = resolved_location
        rules.append(LinkRule(match=match, location_id=location_id, mode=mode))
    return tuple(rules)


def _name_round_trips(location: Location, locations: Mapping[str, Location]) -> bool:
    """Whether writing ``location.name`` parses back to the same location.

    Not when the name holds a separator, reads as a transport mode, or is also
    another location's id, name or alias; the id is written instead then.
    """
    name = location.name
    if "," in name or RULE_SEPARATOR in name or resolve_mode_alias(name) is not None:
        return False
    return _resolve_location(name, locations) == location.id


def format_rules_text(
    rules: Iterable[Mapping[str, object]], locations: Mapping[str, Location]
) -> str:
    """Render stored rules back into the editable ``match => tokens`` text."""
    lines: list[str] = []
    for rule in rules:
        tokens: list[str] = []
        location_id = rule.get("location_id")
        if location_id is not None:
            location = locations.get(str(location_id))
            tokens.append(
                location.name
                if location is not None and _name_round_trips(location, locations)
                else str(location_id)
            )
        if rule.get("mode") is not None:
            tokens.append(str(rule["mode"]))
        lines.append(f"{rule.get('match', '')} {RULE_SEPARATOR} {', '.join(tokens)}")
    return "\n".join(lines)


__all__ = [
    "RULE_SEPARATOR",
    "RulesError",
    "filter_for_link",
    "format_rules_text",
    "parse_rules_text",
    "profile_day_events",
]
