"""Who gets a copy of a notice (feature 7, plan §6.4)."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest
from custom_components.family_departures.models import (
    Margins,
    NotificationIntent,
    ProfileConfig,
)
from custom_components.family_departures.notification_copies import copy_recipients

NOON = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)  # 12:00 local
MIDNIGHT = datetime(2026, 10, 20, 22, 0, tzinfo=UTC)  # 00:00 local, quiet


def _profile(
    pid: str,
    *,
    copy_to: tuple[str, ...] = (),
    copy_kinds: frozenset[str] | None = None,
    notifications_enabled: bool = True,
) -> ProfileConfig:
    kwargs = {} if copy_kinds is None else {"copy_kinds": copy_kinds}
    return ProfileConfig(
        id=pid,
        name=pid.title(),
        default_location_id="school",
        default_mode="car",
        static_minutes=None,
        static_label=None,
        weather_adjust=False,
        car_fallback_minutes=None,
        margins=Margins(
            arrival=5, departure=5, boarding=2, parking_and_walk=0, min_transfer=5
        ),
        weekday_mask=frozenset({0, 1, 2, 3, 4}),
        packing_rules=(),
        person_entity_id=None,
        notifications_enabled=notifications_enabled,
        change_threshold_minutes=3,
        quiet_start=time(22, 0),
        quiet_end=time(6, 0),
        scripts={"push": f"script.push_{pid}"},
        copy_to=copy_to,
        **kwargs,
    )


def _intent(kind: str = "leave_now", person: str = "kid") -> NotificationIntent:
    return NotificationIntent(
        person_id=person,
        mission_id=f"{person}:2026-10-20:morning",
        plan_id="p",
        revision=1,
        notification_id=f"{person}:2026-10-20:morning:{kind}",
        kind=kind,
        severity="info",
        title="t",
        message="m",
        recommended_leave_time=None,
        latest_leave_time=None,
        quality="scheduled",
        reason_codes=(),
        packing_items=(),
        channels=("push",),
        tag="tag",
        action_nonce="n",
    )


def _ids(recipients: list[ProfileConfig]) -> list[str]:
    return [r.id for r in recipients]


def test_a_parent_in_copy_to_gets_a_copy() -> None:
    kid = _profile("kid", copy_to=("parent",))
    profiles = {"kid": kid, "parent": _profile("parent")}
    assert _ids(copy_recipients(_intent(), kid, profiles, NOON)) == ["parent"]


def test_mutual_copies_give_each_exactly_one_copy_and_no_loop() -> None:
    a = _profile("a", copy_to=("b",))
    b = _profile("b", copy_to=("a",))
    profiles = {"a": a, "b": b}
    # The fan-out only follows the original's source, so a copy of a's notice
    # reaches b and is never copied back to a.
    assert _ids(copy_recipients(_intent(person="a"), a, profiles, NOON)) == ["b"]
    assert _ids(copy_recipients(_intent(person="b"), b, profiles, NOON)) == ["a"]


def test_self_copy_is_rejected_by_the_profile() -> None:
    with pytest.raises(ValueError, match="copy_to"):
        _profile("kid", copy_to=("kid",))


def test_unknown_copy_kinds_are_rejected_by_the_profile() -> None:
    with pytest.raises(ValueError, match="copy_kinds"):
        _profile("kid", copy_kinds=frozenset({"leave_now", "nonsense"}))


def test_quiet_hours_drop_a_morning_copy_but_critical_and_cleanup_pass() -> None:
    kid = _profile("kid", copy_to=("parent",))
    profiles = {"kid": kid, "parent": _profile("parent")}
    assert copy_recipients(_intent("morning"), kid, profiles, MIDNIGHT) == []
    assert copy_recipients(_intent("leave_now"), kid, profiles, MIDNIGHT) == []
    assert _ids(copy_recipients(_intent("critical"), kid, profiles, MIDNIGHT)) == [
        "parent"
    ]
    assert _ids(copy_recipients(_intent("cleanup"), kid, profiles, MIDNIGHT)) == [
        "parent"
    ]


def test_a_recipient_with_notifications_off_or_missing_gets_nothing() -> None:
    kid = _profile("kid", copy_to=("quiet", "ghost"))
    profiles = {"kid": kid, "quiet": _profile("quiet", notifications_enabled=False)}
    assert copy_recipients(_intent(), kid, profiles, NOON) == []


def test_copy_kinds_filter_what_is_copied() -> None:
    kid = _profile("kid", copy_to=("parent",), copy_kinds=frozenset({"critical"}))
    profiles = {"kid": kid, "parent": _profile("parent")}
    assert copy_recipients(_intent("leave_now"), kid, profiles, NOON) == []
    assert _ids(copy_recipients(_intent("critical"), kid, profiles, NOON)) == ["parent"]


def test_a_recipient_listed_twice_gets_one_copy() -> None:
    kid = _profile("kid", copy_to=("parent", "parent"))
    profiles = {"kid": kid, "parent": _profile("parent")}
    assert _ids(copy_recipients(_intent(), kid, profiles, NOON)) == ["parent"]


def test_cleanup_is_copied_even_when_not_ticked_but_not_if_nothing_is_copied() -> None:
    profiles_for = lambda kid: {"kid": kid, "parent": _profile("parent")}  # noqa: E731
    kid = _profile("kid", copy_to=("parent",), copy_kinds=frozenset({"leave_now"}))
    assert _ids(copy_recipients(_intent("cleanup"), kid, profiles_for(kid), NOON)) == [
        "parent"
    ]
    assert copy_recipients(_intent("morning"), kid, profiles_for(kid), NOON) == []
    silent = _profile("kid", copy_to=("parent",), copy_kinds=frozenset())
    assert copy_recipients(_intent("cleanup"), silent, profiles_for(silent), NOON) == []
