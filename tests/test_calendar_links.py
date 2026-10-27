"""Tests for per-link event filtering and attendee matching (plan §4.1, §4.5).

These assert the product behaviour the plan calls out for a shared calendar:

* per-link include/exclude summary patterns select different events from the
  same fetched day;
* ``attendee_mode`` applies the ``vem:`` tags: ``tagged_or_untagged`` lets an
  untagged event reach everyone while a tagged one reaches only the named
  person, ``tagged`` requires a match, and ``all`` ignores tags;
* matching is casefolded so a profile's aliases (display name, person friendly
  name, person object id) all work.

The filtering itself is pure; only the alias *resolution* needs Home Assistant,
and that is covered in ``test_coordinator`` via the coordinator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from custom_components.family_departures.calendar_links import (
    RulesError,
    filter_for_link,
    format_rules_text,
    parse_rules_text,
)
from custom_components.family_departures.models import (
    CalendarLink,
    EventTags,
    LinkRule,
    Location,
    ScheduleEvent,
    SourceFilter,
)

START = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


def _event(
    uid: str, summary: str, *, attendees: tuple[str, ...] = (), minutes: int = 0
) -> ScheduleEvent:
    start = START + timedelta(minutes=minutes)
    return ScheduleEvent(
        uid=uid,
        summary=summary,
        start=start,
        end=start + timedelta(hours=1),
        source_id="shared_calendar",
        tags=EventTags(attendees=attendees),
    )


def _link(
    link_id: str,
    *,
    attendee_mode: str = "all",
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
) -> CalendarLink:
    return CalendarLink(
        link_id=link_id,
        calendar_id="shared_calendar",
        role="extra",
        source_filter=SourceFilter(include_patterns=include, exclude_patterns=exclude),
        attendee_mode=attendee_mode,
    )


FOOTBALL = _event("e1", "Fotboll")
SWIM = _event("e2", "Simning", minutes=30)
TAGGED_A = _event("e3", "Pianolektion", attendees=("Barn A",), minutes=60)


def test_include_patterns_select_different_events_for_two_links() -> None:
    """Two links over the same day keep different events by include pattern."""
    events = (FOOTBALL, SWIM)
    link_football = _link("l_football", include=("fotboll",))
    link_swim = _link("l_swim", include=("simning",))

    assert filter_for_link(events, link_football) == (FOOTBALL,)
    assert filter_for_link(events, link_swim) == (SWIM,)


def test_exclude_pattern_drops_matching_event() -> None:
    """An exclude pattern removes the matching event."""
    events = (FOOTBALL, SWIM)
    link = _link("l", exclude=("simning",))
    assert filter_for_link(events, link) == (FOOTBALL,)


def test_attendee_mode_all_ignores_tags() -> None:
    """``all`` keeps tagged and untagged events alike."""
    events = (FOOTBALL, TAGGED_A)
    link = _link("l", attendee_mode="all")
    assert filter_for_link(events, link, frozenset({"barn b"})) == events


def test_tagged_or_untagged_untagged_reaches_everyone() -> None:
    """An untagged event reaches a profile that is not named."""
    events = (FOOTBALL, TAGGED_A)
    link = _link("l", attendee_mode="tagged_or_untagged")
    # Barn B is not named on the tagged event, so only the untagged one applies.
    kept = filter_for_link(events, link, frozenset({"barn b"}))
    assert kept == (FOOTBALL,)


def test_tagged_or_untagged_tagged_reaches_only_named_person() -> None:
    """A tagged event reaches only the named person; untagged reaches both."""
    events = (FOOTBALL, TAGGED_A)
    link = _link("l", attendee_mode="tagged_or_untagged")
    kept_a = filter_for_link(events, link, frozenset({"barn a"}))
    assert kept_a == (FOOTBALL, TAGGED_A)


def test_tagged_requires_a_match() -> None:
    """``tagged`` drops untagged events and keeps only named ones."""
    events = (FOOTBALL, TAGGED_A)
    link = _link("l", attendee_mode="tagged")
    assert filter_for_link(events, link, frozenset({"barn a"})) == (TAGGED_A,)
    assert filter_for_link(events, link, frozenset({"barn b"})) == ()


def test_attendee_match_is_casefolded() -> None:
    """Attendee matching is case-insensitive on both sides."""
    event = _event("e", "Pianolektion", attendees=("BARN A",))
    link = _link("l", attendee_mode="tagged")
    assert filter_for_link((event,), link, frozenset({"barn a"})) == (event,)


# --- Rules text (plan §5.2) --------------------------------------------------

_LOCATIONS = {
    "hall": Location("hall", "Sporthallen", 1.0, 2.0, aliases=("Idrottshallen",)),
    "pool": Location("pool", "Badhuset", 3.0, 4.0),
}


def test_rules_text_resolves_modes_before_locations_and_skips_blank_lines() -> None:
    rules = parse_rules_text(
        "IDRO => sporthallen\n\nSimning => badhuset, kollektivt\nCykel => cykel",
        _LOCATIONS,
    )
    assert rules == (
        LinkRule("IDRO", location_id="hall", mode=None),
        LinkRule("Simning", location_id="pool", mode="public_transport"),
        LinkRule("Cykel", location_id=None, mode="static"),
    )


def test_rules_text_matches_location_alias_and_id() -> None:
    assert parse_rules_text("A => Idrottshallen", _LOCATIONS)[0].location_id == "hall"
    assert parse_rules_text("A => pool", _LOCATIONS)[0].location_id == "pool"


@pytest.mark.parametrize(
    "text",
    [
        "no separator here",
        " => Sporthallen",
        "IDRO =>",
        "IDRO => Okänd plats",
        "IDRO => bil, kollektivt",
        "IDRO => sporthallen, badhuset",
    ],
)
def test_rules_text_rejects_bad_lines(text: str) -> None:
    with pytest.raises(RulesError):
        parse_rules_text(text, _LOCATIONS)


def test_format_rules_text_round_trips() -> None:
    text = "IDRO => Sporthallen\nSimning => Badhuset, public_transport"
    rules = parse_rules_text(text, _LOCATIONS)
    stored = [
        {"match": r.match, "location_id": r.location_id, "mode": r.mode} for r in rules
    ]
    assert parse_rules_text(format_rules_text(stored, _LOCATIONS), _LOCATIONS) == rules


def test_rules_text_writes_the_id_when_a_name_would_not_round_trip() -> None:
    locations = {
        "school": Location("school", "Skolan", 1.0, 2.0),
        "skolan_2": Location("skolan_2", "Skolan", 3.0, 4.0),
        "solna": Location("solna", "Hallen, Solna", 5.0, 6.0),
        "cykel_hall": Location("cykel_hall", "Cykel", 7.0, 8.0),
    }
    for location_id, expected in (
        ("school", "Skolan"),
        ("skolan_2", "skolan_2"),
        ("solna", "solna"),
        ("cykel_hall", "cykel_hall"),
    ):
        stored = [{"match": "X", "location_id": location_id, "mode": None}]
        text = format_rules_text(stored, locations)
        assert text == f"X => {expected}"
        [rule] = parse_rules_text(text, locations)
        assert rule.location_id == location_id
