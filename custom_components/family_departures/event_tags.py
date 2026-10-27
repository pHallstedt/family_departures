"""Entry-level override parsing (features 3 and 4, plan §5.1).

A calendar entry can override its destination, transport mode and the persons
it applies to. For calendars the household controls (Local Calendar and any
feed the user can edit) the convention is one ``key: value`` tag per DESCRIPTION
line, with a secondary inline ``[key: value]`` bracket form in the SUMMARY for
apps where editing the description is awkward.

This module is *pure*: no ``homeassistant`` import, no network, no ``now``
dependency. It only recognises known keys and resolves mode aliases; the raw
location and attendee values are resolved to real ids elsewhere (plan §4.7).

Privacy (plan §5.3, §10, review-privacy-security): the raw DESCRIPTION is never
returned, stored, displayed or logged. Only the parsed :class:`EventTags` leave
this module.
"""

from __future__ import annotations

import re

from .models import EventTags, Mode

# Recognised tag keys, casefolded, grouped by the field they set. Several
# Swedish/English aliases map to the same field (plan §5.1).
_LOCATION_KEYS: frozenset[str] = frozenset(("plats", "location", "destination"))
_TRANSPORT_KEYS: frozenset[str] = frozenset(("färdsätt", "transport", "mode"))
# ``för`` is deliberately not a key: ``För: alla barn i åk 3`` is ordinary
# prose, and parsing it would hide the event from every person (plan §5.1 lists
# ``vem:`` and ``who:`` only; revised after review).
_ATTENDEE_KEYS: frozenset[str] = frozenset(("vem", "who"))
# An attendee list that names everyone means "no filter".
_EVERYONE: frozenset[str] = frozenset(("alla", "all", "everyone"))

# Mode aliases (casefolded) -> canonical :data:`models.Mode`. Unknown values
# resolve to ``None`` so the next precedence level applies (plan §4.7, §5.3).
MODE_ALIASES: dict[str, Mode] = {
    "kollektivt": "public_transport",
    "kollektiv": "public_transport",
    "sl": "public_transport",
    "buss": "public_transport",
    "tåg": "public_transport",
    "tunnelbana": "public_transport",
    "transit": "public_transport",
    "public_transport": "public_transport",
    "bil": "car",
    "car": "car",
    "cykel": "static",
    "cykla": "static",
    "gå": "static",
    "gång": "static",
    "promenad": "static",
    "bike": "static",
    "walk": "static",
    "static": "static",
}

# ``[key: value]`` anywhere in the summary. The value runs up to the closing
# bracket. Keys/values are stripped and interpreted case-insensitively.
_BRACKET_TAG_RE = re.compile(r"\[\s*([^:\]]+?)\s*:\s*([^\]]*?)\s*\]")

# ``key: value`` on a single description line. Only the first colon splits, so
# a value may itself contain a colon.
_LINE_TAG_RE = re.compile(r"^\s*([^:]+?)\s*:\s*(.*?)\s*$")


def resolve_mode_alias(value: str | None) -> Mode | None:
    """Resolve a transport tag value to a canonical mode, or ``None``.

    Matching is casefolded and trims surrounding whitespace. An unknown value
    returns ``None`` so the caller falls through to the next precedence level.
    """
    if value is None:
        return None
    return MODE_ALIASES.get(value.strip().casefold())


def strip_tags(summary: str) -> str:
    """Return ``summary`` with any ``[key: value]`` bracket tags removed.

    Only recognised keys are stripped, so unrelated bracketed text (e.g.
    ``[AVBOKAD]``) is preserved. Collapses the whitespace left behind.
    """

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1).strip().casefold()
        if key in _LOCATION_KEYS or key in _TRANSPORT_KEYS or key in _ATTENDEE_KEYS:
            return " "
        return match.group(0)

    stripped = _BRACKET_TAG_RE.sub(_replace, summary)
    return re.sub(r"\s{2,}", " ", stripped).strip()


def _split_attendees(value: str) -> tuple[str, ...]:
    """Split a ``vem:`` comma list into trimmed, non-empty names."""
    names = tuple(part.strip() for part in value.split(",") if part.strip())
    if any(name.casefold() in _EVERYONE for name in names):
        return ()
    return names


def _apply_tag(
    key: str,
    value: str,
    location: str | None,
    transport: str | None,
    attendees: tuple[str, ...],
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Fold one recognised ``key: value`` tag into the running result.

    First occurrence wins: a field already set is never overwritten. Unknown
    keys and empty values are ignored.
    """
    folded = key.strip().casefold()
    trimmed = value.strip()
    if not trimmed:
        return location, transport, attendees
    if folded in _LOCATION_KEYS and location is None:
        location = trimmed
    elif folded in _TRANSPORT_KEYS and transport is None:
        transport = trimmed
    elif folded in _ATTENDEE_KEYS and not attendees:
        attendees = _split_attendees(trimmed)
    return location, transport, attendees


def parse_event_tags(summary: str | None, description: str | None) -> EventTags:
    """Parse override tags from an event's summary and description (plan §5.1).

    DESCRIPTION is scanned line by line for ``key: value`` tags; the SUMMARY is
    scanned for inline ``[key: value]`` bracket tags. Keys are case-insensitive
    and the first occurrence of each field wins. Description tags are read
    before summary bracket tags, so a DESCRIPTION value takes precedence.

    The transport value is left raw here (resolved with
    :func:`resolve_mode_alias` by the caller) so an unknown mode can still fall
    through the precedence chain.
    """
    location: str | None = None
    transport: str | None = None
    attendees: tuple[str, ...] = ()

    if description:
        for line in description.splitlines():
            match = _LINE_TAG_RE.match(line)
            if match is None:
                continue
            location, transport, attendees = _apply_tag(
                match.group(1), match.group(2), location, transport, attendees
            )

    if summary:
        for match in _BRACKET_TAG_RE.finditer(summary):
            location, transport, attendees = _apply_tag(
                match.group(1), match.group(2), location, transport, attendees
            )

    return EventTags(location=location, transport=transport, attendees=attendees)
