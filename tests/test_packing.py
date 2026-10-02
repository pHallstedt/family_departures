"""Tests for packing-list building (spec §5.5).

These assert the product behaviour from §5.5 and the T06 acceptance rows with
synthetic events (no real schedule content): a PE lesson anywhere in the day
yields a reminder, duplicates collapse, attendance overrides empty the list,
acknowledgements hide only the items present at the time, and a removed lesson
removes its line.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from custom_components.family_departures.models import (
    DayOverride,
    PackingRule,
    ScheduleEvent,
)
from custom_components.family_departures.packing import build_packing_list

TZ = ZoneInfo("Europe/Stockholm")
DAY = date(2026, 10, 1)
GYM = PackingRule(id="gym_clothes", match="IDRO", item="Gympakläder")
BOOK = PackingRule(id="library", match="BIBL", item="Biblioteksbok")


def _local(hh: int, mm: int) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hh, mm, tzinfo=TZ).astimezone(UTC)


def _event(hh: int, *, summary: str, uid: str) -> ScheduleEvent:
    return ScheduleEvent(
        uid=uid,
        summary=summary,
        start=_local(hh, 0),
        end=_local(hh + 1, 0),
        source_id="kid_b_ics",
    )


def _build(
    events: tuple[ScheduleEvent, ...],
    *,
    rules: tuple[PackingRule, ...] = (GYM,),
    override: DayOverride | None = None,
    acknowledged: tuple[str, ...] = (),
):
    return build_packing_list(
        person_id="kid_b",
        local_date=DAY,
        events=events,
        rules=rules,
        override=override,
        acknowledged=acknowledged,
    )


def test_pe_as_third_lesson_yields_item() -> None:
    """A PE lesson that is not the first of the day still packs gym clothes."""
    events = (
        _event(8, summary="Lektion MA", uid="a"),
        _event(10, summary="Lektion SV", uid="b"),
        _event(14, summary="Lektion IDRO1000X", uid="c"),
    )
    result = _build(events)
    assert result.items == ("Gympakläder",)
    assert result.person_id == "kid_b"
    assert result.local_date == DAY


def test_two_pe_lessons_collapse_to_one_item() -> None:
    """Several matching lessons the same day give a single line."""
    events = (
        _event(8, summary="Lektion IDRO1000X", uid="a"),
        _event(13, summary="Lektion IDRO2000Y", uid="b"),
    )
    result = _build(events)
    assert result.items == ("Gympakläder",)


def test_match_is_case_insensitive() -> None:
    events = (_event(9, summary="lektion idro1000x", uid="a"),)
    result = _build(events)
    assert result.items == ("Gympakläder",)


def test_no_matching_lesson_gives_empty_list() -> None:
    events = (
        _event(8, summary="Lektion MA", uid="a"),
        _event(10, summary="Lektion SV", uid="b"),
    )
    result = _build(events)
    assert result.items == ()
    assert result.acknowledged == ()


def test_sick_override_empties_list() -> None:
    """An off/sick/remote override empties the list regardless of lessons."""
    events = (_event(9, summary="Lektion IDRO1000X", uid="a"),)
    override = DayOverride(person_id="kid_b", local_date=DAY, attendance="sick")
    result = _build(events, override=override)
    assert result.items == ()
    assert result.acknowledged == ()


def test_off_and_remote_overrides_also_empty_list() -> None:
    events = (_event(9, summary="Lektion IDRO1000X", uid="a"),)
    for attendance in ("off", "remote"):
        override = DayOverride(
            person_id="kid_b",
            local_date=DAY,
            attendance=attendance,  # type: ignore[arg-type]
        )
        result = _build(events, override=override)
        assert result.items == (), attendance


def test_normal_override_does_not_empty_list() -> None:
    events = (_event(9, summary="Lektion IDRO1000X", uid="a"),)
    override = DayOverride(person_id="kid_b", local_date=DAY, attendance="normal")
    result = _build(events, override=override)
    assert result.items == ("Gympakläder",)


def test_acknowledged_item_is_excluded_but_new_item_still_shown() -> None:
    """Acked items drop out of pending; a later rule match still appears."""
    events = (
        _event(8, summary="Lektion IDRO1000X", uid="a"),
        _event(10, summary="Lektion BIBL", uid="b"),
    )
    result = _build(events, rules=(GYM, BOOK), acknowledged=("Gympakläder",))
    assert result.items == ("Biblioteksbok",)
    assert result.acknowledged == ("Gympakläder",)


def test_acknowledgement_trimmed_when_lesson_removed() -> None:
    """A removed lesson removes its line and its stale acknowledgement."""
    # Gym lesson gone this fetch; only the library lesson remains.
    events = (_event(10, summary="Lektion BIBL", uid="b"),)
    result = _build(events, rules=(GYM, BOOK), acknowledged=("Gympakläder",))
    assert result.items == ("Biblioteksbok",)
    assert result.acknowledged == ()


def test_removed_lesson_removes_item() -> None:
    """When the matching lesson disappears, its item is no longer listed."""
    with_pe = _build((_event(9, summary="Lektion IDRO1000X", uid="a"),))
    assert with_pe.items == ("Gympakläder",)

    without_pe = _build((_event(9, summary="Lektion MA", uid="a"),))
    assert without_pe.items == ()


def test_items_preserve_first_match_order() -> None:
    events = (
        _event(8, summary="Lektion BIBL", uid="a"),
        _event(10, summary="Lektion IDRO1000X", uid="b"),
    )
    result = _build(events, rules=(GYM, BOOK))
    assert result.items == ("Biblioteksbok", "Gympakläder")


def test_empty_schedule_gives_empty_list() -> None:
    result = _build(())
    assert result.items == ()
    assert result.acknowledged == ()
