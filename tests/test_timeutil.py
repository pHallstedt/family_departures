"""Tests for the pure time utilities."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest
from custom_components.family_departures.timeutil import (
    TZ,
    combine_local,
    local_date_of,
    local_day_bounds,
    make_mission_id,
)


def test_local_date_of_naive_raises() -> None:
    with pytest.raises(ValueError):
        local_date_of(datetime(2026, 10, 1, 6, 0))


def test_local_date_of_around_midnight_utc() -> None:
    # 2026-09-30 23:30 UTC is 2026-10-01 01:30 local (CEST, +2) -> next day.
    dt = datetime(2026, 9, 30, 23, 30, tzinfo=UTC)
    assert local_date_of(dt) == date(2026, 10, 1)


def test_local_date_of_just_before_local_midnight() -> None:
    # 2026-10-01 21:30 UTC is 2026-10-01 23:30 local -> same day.
    dt = datetime(2026, 10, 1, 21, 30, tzinfo=UTC)
    assert local_date_of(dt) == date(2026, 10, 1)


def test_local_date_of_accepts_non_utc_aware() -> None:
    local = datetime(2026, 10, 1, 0, 30, tzinfo=TZ)
    assert local_date_of(local) == date(2026, 10, 1)


def test_local_date_of_spring_forward_morning() -> None:
    # 2026-03-29 05:30 UTC is 07:30 local CEST (+2) on the spring-forward day.
    dt = datetime(2026, 3, 29, 5, 30, tzinfo=UTC)
    assert local_date_of(dt) == date(2026, 3, 29)


def test_local_date_of_fall_back_morning() -> None:
    # 2026-10-25 06:30 UTC is 07:30 local CET (+1) on the fall-back day.
    dt = datetime(2026, 10, 25, 6, 30, tzinfo=UTC)
    assert local_date_of(dt) == date(2026, 10, 25)


def test_local_day_bounds_summer_is_22_hours_utc() -> None:
    # A normal CEST day spans 22:00 UTC (prev) to 22:00 UTC = 24 local hours.
    start, end = local_day_bounds(date(2026, 7, 1))
    assert start == datetime(2026, 6, 30, 22, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 1, 22, 0, tzinfo=UTC)
    assert (end - start).total_seconds() == 24 * 3600


def test_local_day_bounds_spring_forward_is_23_hours() -> None:
    # 2026-03-29 the clocks jump 02:00 -> 03:00; the local day is 23 hours.
    start, end = local_day_bounds(date(2026, 3, 29))
    assert (end - start).total_seconds() == 23 * 3600


def test_local_day_bounds_fall_back_is_25_hours() -> None:
    # 2026-10-25 the clocks fall back 03:00 -> 02:00; the local day is 25 hours.
    start, end = local_day_bounds(date(2026, 10, 25))
    assert (end - start).total_seconds() == 25 * 3600


def test_combine_local_summer_offset() -> None:
    # 08:20 local on 2026-07-01 (CEST +2) is 06:20 UTC.
    assert combine_local(date(2026, 7, 1), time(8, 20)) == datetime(
        2026, 7, 1, 6, 20, tzinfo=UTC
    )


def test_combine_local_winter_offset() -> None:
    # 08:20 local on 2026-01-15 (CET +1) is 07:20 UTC.
    assert combine_local(date(2026, 1, 15), time(8, 20)) == datetime(
        2026, 1, 15, 7, 20, tzinfo=UTC
    )


def test_combine_local_dst_transition_day() -> None:
    # After the spring-forward on 2026-03-29, 08:20 local is CEST (+2) -> 06:20 UTC.
    assert combine_local(date(2026, 3, 29), time(8, 20)) == datetime(
        2026, 3, 29, 6, 20, tzinfo=UTC
    )


def test_make_mission_id_default_slot() -> None:
    assert make_mission_id("kid_a", date(2026, 10, 1)) == "kid_a:2026-10-01:morning"


def test_make_mission_id_explicit_slot() -> None:
    assert make_mission_id("parent_b", date(2026, 10, 1), "evening") == (
        "parent_b:2026-10-01:evening"
    )
