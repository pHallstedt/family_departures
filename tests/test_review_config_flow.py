"""Review findings for the options-flow rewrite (each test fails on a real defect)."""

from __future__ import annotations

from typing import Any

from custom_components.family_departures.const import OPT_PROFILES
from custom_components.family_departures.household import parse_household
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from test_config_flow import (
    _MARGINS,
    _NOTIFICATIONS,
    CLUB_CAL,
    HALL,
    LINK_FIELDS,
    SCHOOL,
    SCHOOL_CAL,
    _link,
    _open,
    _profile_v2,
    _setup_entry,
    _submit,
    _suggested,
)


async def _edit_through(
    hass: HomeAssistant,
    entry: Any,
    profile_id: str,
    *,
    calendars: list[str],
    main: str | None,
    links: dict[str, dict[str, Any]] | None = None,
    use_suggested_rules: bool = False,
) -> dict:
    """Click through an edit of ``profile_id``; return the result after the links."""
    pick = await _open(hass, entry, "edit_profile")
    person = await _submit(hass, pick, {"profile_id": profile_id})
    dest = await _submit(hass, person, {"is_adult": False})
    cals = await _submit(hass, dest, {"default_mode": "car", "weather_adjust": False})
    cal_input: dict[str, Any] = {"calendars": calendars}
    if main:
        cal_input["main_calendar"] = main
    result = await _submit(hass, cals, cal_input)
    for calendar_id in calendars:
        assert result["step_id"] == "profile_link", result.get("errors")
        fields = {**LINK_FIELDS, **(links or {}).get(calendar_id, {})}
        if use_suggested_rules:
            fields["rules"] = _suggested(result["data_schema"], "rules")
        result = await _submit(hass, result, fields)
    return result


async def _finish_edit(hass: HomeAssistant, result: dict) -> dict:
    assert result["step_id"] == "profile_margins", result.get("errors")
    packing = await _submit(hass, result, _MARGINS)
    notif = await _submit(hass, packing, {"packing_match": "", "packing_item": ""})
    test = await _submit(hass, notif, _NOTIFICATIONS)
    done = await _submit(hass, test)
    await hass.async_block_till_done()
    return done


async def test_unedited_rules_keep_pointing_at_the_same_location_when_names_repeat(
    hass: HomeAssistant,
) -> None:
    """Rules are round-tripped by location *name*; a repeated name rebinds them."""
    other_school = {**SCHOOL, "id": "skolan_2", "lat": 59.9}  # same name "Skolan"
    link = _link(
        "club",
        "club_cal",
        "extra",
        rules=[{"match": "Simhall", "location_id": "skolan_2", "mode": None}],
    )
    entry = await _setup_entry(
        hass,
        profiles={"kid_a": _profile_v2("kid_a", links=(link,), location="hall")},
        locations={"school": SCHOOL, "skolan_2": other_school, "hall": HALL},
        calendars={"club_cal": CLUB_CAL},
    )
    result = await _edit_through(
        hass,
        entry,
        "kid_a",
        calendars=["club_cal"],
        main=None,
        use_suggested_rules=True,
    )
    await _finish_edit(hass, result)

    [saved] = entry.options[OPT_PROFILES]["kid_a"]["calendar_links"]
    assert saved["rules"][0]["location_id"] == "skolan_2", (
        "clicking through an edit silently re-pointed the rule to "
        f"{saved['rules'][0]['location_id']!r}"
    )


async def test_location_name_with_a_comma_does_not_block_saving_unedited_rules(
    hass: HomeAssistant,
) -> None:
    """The rules text splits tokens on commas, so such a name can never re-parse."""
    solna = {**HALL, "id": "solna", "name": "Hallen, Solna", "aliases": []}
    link = _link(
        "club",
        "club_cal",
        "extra",
        rules=[{"match": "Simhall", "location_id": "solna", "mode": None}],
    )
    entry = await _setup_entry(
        hass,
        profiles={"kid_a": _profile_v2("kid_a", links=(link,))},
        locations={"school": SCHOOL, "solna": solna},
        calendars={"club_cal": CLUB_CAL},
    )
    result = await _edit_through(
        hass,
        entry,
        "kid_a",
        calendars=["club_cal"],
        main=None,
        use_suggested_rules=True,
    )
    assert result["step_id"] == "profile_margins", (
        f"stuck on {result['step_id']} with errors {result.get('errors')}"
    )


async def test_link_ids_stay_unique_when_a_new_link_takes_an_old_link_id(
    hass: HomeAssistant,
) -> None:
    """A calendar with id 'main' gets link id 'main' while an old link keeps it."""
    old_main = _link("main", "a_cal", "main")
    entry = await _setup_entry(
        hass,
        profiles={"kid_a": _profile_v2("kid_a", links=(old_main,))},
        locations={"school": SCHOOL},
        calendars={
            "a_cal": {**SCHOOL_CAL, "id": "a_cal"},
            "main": {**CLUB_CAL, "id": "main", "name": "Main"},
        },
    )
    result = await _edit_through(
        hass, entry, "kid_a", calendars=["main", "a_cal"], main="a_cal"
    )
    await _finish_edit(hass, result)

    links = entry.options[OPT_PROFILES]["kid_a"]["calendar_links"]
    ids = [link["link_id"] for link in links]
    assert len(ids) == len(set(ids)), f"duplicate link ids {ids}"
    household = parse_household(entry.data, entry.options)
    assert "kid_a" in household.profiles, "saved profile fails to parse (skipped)"


async def test_choosing_new_calendar_as_main_without_a_new_calendar_is_an_error(
    hass: HomeAssistant,
) -> None:
    """Picking '(new calendar)' as main but naming none must not silently drop main."""
    entry = await _setup_entry(
        hass, locations={"school": SCHOOL}, calendars={"school_cal": SCHOOL_CAL}
    )
    result = await _open(hass, entry, "add_profile")
    result = await _submit(
        hass,
        result,
        {"person_entity_id": "person.kid_a", "name": "Kid A", "is_adult": False},
    )
    result = await _submit(
        hass,
        result,
        {
            "default_location": "school",
            "default_mode": "car",
            "weather_adjust": False,
        },
    )
    result = await _submit(
        hass,
        result,
        {"calendars": ["school_cal"], "main_calendar": "new-calendar"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "profile_calendars", (
        f"accepted and moved on to {result['step_id']} with no main calendar"
    )


async def test_demoted_main_link_on_a_shared_calendar_is_protected(
    hass: HomeAssistant,
) -> None:
    """A main link that becomes extra on a calendar kid_b also links stays 'all'."""
    kid_a_main = _link("club", "club_cal", "main", attendee_mode="all")
    kid_b_extra = _link("club", "club_cal", "extra", attendee_mode="tagged_or_untagged")
    entry = await _setup_entry(
        hass,
        profiles={
            "kid_a": _profile_v2("kid_a", links=(kid_a_main,)),
            "kid_b": _profile_v2("kid_b", links=(kid_b_extra,)),
        },
        locations={"school": SCHOOL},
        calendars={"club_cal": CLUB_CAL, "school_cal": SCHOOL_CAL},
    )
    result = await _edit_through(
        hass,
        entry,
        "kid_a",
        calendars=["club_cal", "school_cal"],
        main="school_cal",
    )
    await _finish_edit(hass, result)

    kid_a = entry.options[OPT_PROFILES]["kid_a"]["calendar_links"]
    club = next(link for link in kid_a if link["calendar_id"] == "club_cal")
    assert club["role"] == "extra"
    assert club["attendee_mode"] != "all", (
        "kid_a's demoted link still matches events tagged for kid_b"
    )
