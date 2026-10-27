"""Tests for entry-level override tag parsing (features 3/4, plan §5.1).

These assert the product behaviour the plan calls out: Swedish/English keys,
case-insensitivity, first-occurrence-wins, unknown mode -> None, the inline
bracket form, and that the raw DESCRIPTION never leaks (only parsed tags do).
"""

from __future__ import annotations

from custom_components.family_departures.event_tags import (
    MODE_ALIASES,
    parse_event_tags,
    resolve_mode_alias,
    strip_tags,
)


def test_description_location_sv_and_en_keys() -> None:
    """Each of the location key aliases sets the location."""
    assert parse_event_tags(None, "plats: Badhuset").location == "Badhuset"
    assert parse_event_tags(None, "location: Pool").location == "Pool"
    assert parse_event_tags(None, "destination: Hall").location == "Hall"


def test_description_transport_sv_and_en_keys() -> None:
    """Each of the transport key aliases sets the raw transport value."""
    assert parse_event_tags(None, "färdsätt: cykel").transport == "cykel"
    assert parse_event_tags(None, "transport: car").transport == "car"
    assert parse_event_tags(None, "mode: sl").transport == "sl"


def test_attendee_key_aliases_split_comma_list() -> None:
    """vem/who parse a comma list into trimmed names."""
    for key in ("vem", "who"):
        tags = parse_event_tags(None, f"{key}: Barn A, Barn B")
        assert tags.attendees == ("Barn A", "Barn B")


def test_keys_are_case_insensitive() -> None:
    """Key matching is casefolded."""
    tags = parse_event_tags(None, "PLATS: Hall\nFÄRDSÄTT: Bil\nVEM: Barn A")
    assert tags.location == "Hall"
    assert tags.transport == "Bil"
    assert tags.attendees == ("Barn A",)


def test_first_occurrence_wins() -> None:
    """A repeated key keeps the first value, per field."""
    tags = parse_event_tags(None, "plats: First\nplats: Second")
    assert tags.location == "First"


def test_empty_value_is_ignored() -> None:
    """A tag with no value leaves the field unset."""
    assert parse_event_tags(None, "plats:   ").location is None


def test_unknown_key_is_ignored() -> None:
    """A line that is not a recognised tag is ignored."""
    tags = parse_event_tags(None, "Tränare: Anna\nplats: Hall")
    assert tags.location == "Hall"
    assert tags.attendees == ()


def test_bracket_form_in_summary() -> None:
    """The inline [key: value] form sets tags."""
    tags = parse_event_tags("Simning [plats: Badhuset] [färdsätt: kollektivt]", None)
    assert tags.location == "Badhuset"
    assert tags.transport == "kollektivt"


def test_description_takes_precedence_over_summary_bracket() -> None:
    """A DESCRIPTION tag wins over a SUMMARY bracket tag (first wins)."""
    tags = parse_event_tags("Fotboll [plats: Summary]", "plats: Description")
    assert tags.location == "Description"


def test_strip_tags_removes_recognised_bracket_tags() -> None:
    """strip_tags removes recognised bracket tags and collapses whitespace."""
    assert strip_tags("Simning [plats: Badhuset] [färdsätt: kollektivt]") == "Simning"


def test_strip_tags_preserves_unrelated_brackets() -> None:
    """Non-tag bracketed text (e.g. [AVBOKAD]) is kept."""
    assert strip_tags("Fotboll [AVBOKAD]") == "Fotboll [AVBOKAD]"


def test_resolve_mode_alias_known_and_unknown() -> None:
    """Known aliases resolve to a canonical mode; unknown -> None."""
    assert resolve_mode_alias("cykel") == "static"
    assert resolve_mode_alias("Bil") == "car"
    assert resolve_mode_alias(" SL ") == "public_transport"
    assert resolve_mode_alias("teleport") is None
    assert resolve_mode_alias(None) is None


def test_mode_aliases_cover_three_canonical_modes() -> None:
    """The alias table maps only to the three canonical modes."""
    assert set(MODE_ALIASES.values()) == {"public_transport", "car", "static"}


def test_raw_description_is_not_returned() -> None:
    """Only parsed EventTags leave the parser; the raw text never does."""
    description = "plats: Hall\nHemlig anteckning om barnet"
    tags = parse_event_tags("Fotboll", description)
    # The returned value exposes only the parsed fields, never the raw note.
    assert "Hemlig" not in repr(tags)
    assert tags.location == "Hall"


def test_plain_swedish_sentence_is_not_an_attendee_tag() -> None:
    """A natural description line starting with "För:" must not hide the event
    from every person by naming a non-existent attendee."""
    tags = parse_event_tags(None, "För: alla barn i åk 3")
    assert tags.attendees == ()


def test_attendee_list_naming_everyone_is_not_a_filter() -> None:
    for line in ("vem: alla", "who: Everyone", "vem: Barn A, alla"):
        assert parse_event_tags(None, line).attendees == ()
    assert parse_event_tags(None, "vem: Barn A, Barn B").attendees == (
        "Barn A",
        "Barn B",
    )
