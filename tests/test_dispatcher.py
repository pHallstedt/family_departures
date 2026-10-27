"""Tests for the notification dispatcher and companion actions (spec §12.2–§12.4).

The dispatcher is the delivery half of the notification path: the pure policy
(T15) decided *what* to send and the scheduler (T17) decided *when*; here we
check *how* an intent reaches the configured channel scripts, how the global
dry-run and test-recipient toggles reroute it, that one failing channel never
stops the others or the dashboard, and that a companion-app action only mutates
state when its mission and nonce are current.

These use the PHACC ``hass`` fixture with a real :class:`FamilyDeparturesStore`,
record ``script.turn_on`` calls with a stub service, and fire the companion
``mobile_app_notification_action`` event directly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from custom_components.family_departures.const import (
    EVENT_NOTIFICATION,
    MOBILE_APP_ACTION_EVENT,
)
from custom_components.family_departures.dispatcher import NotificationDispatcher
from custom_components.family_departures.models import (
    CalendarLink,
    MissionState,
    NotificationIntent,
    PackingRule,
    ProfileConfig,
    ScheduleEvent,
    ScheduleResult,
)
from custom_components.family_departures.store import FamilyDeparturesStore
from freezegun import freeze_time
from homeassistant.core import HomeAssistant, ServiceResponse, SupportsResponse
from homeassistant.util import dt as dt_util

TODAY = date(2026, 10, 20)
MISSION_ID = f"kid_a:{TODAY.isoformat()}:morning"
PARENT_B_MISSION_ID = f"parent_b:{TODAY.isoformat()}:morning"
NONCE = "nonce-abc"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _margins() -> Any:
    from custom_components.family_departures.models import Margins

    return Margins(
        arrival=5, departure=5, boarding=2, parking_and_walk=0, min_transfer=5
    )


def _profile(
    profile_id: str = "kid_a",
    *,
    name: str | None = None,
    scripts: dict[str, str] | None = None,
    packing_rules: tuple[PackingRule, ...] = (),
    copy_to: tuple[str, ...] = (),
    notifications_enabled: bool = True,
) -> ProfileConfig:
    from datetime import time as _t

    return ProfileConfig(
        id=profile_id,
        name=name if name is not None else profile_id.replace("_", " ").title(),
        default_location_id=f"{profile_id}_destination",
        default_mode="public_transport",
        static_minutes=None,
        static_label=None,
        weather_adjust=False,
        car_fallback_minutes=None,
        margins=_margins(),
        weekday_mask=frozenset({0, 1, 2, 3, 4}),
        packing_rules=packing_rules,
        person_entity_id=None,
        notifications_enabled=notifications_enabled,
        change_threshold_minutes=3,
        quiet_start=_t.fromisoformat("22:00:00"),
        quiet_end=_t.fromisoformat("06:00:00"),
        scripts=(
            scripts if scripts is not None else {"push": f"script.push_{profile_id}"}
        ),
        calendar_links=(
            CalendarLink(
                link_id="main",
                calendar_id=f"{profile_id}_calendar",
                role="main",
            ),
        ),
        evening_notice_enabled=True,
        copy_to=copy_to,
    )


def _intent(
    *,
    person_id: str = "kid_a",
    mission_id: str = MISSION_ID,
    kind: str = "leave_now",
    channels: tuple[str, ...] = ("push",),
    packing_items: tuple[str, ...] = (),
    lunch_menu: str | None = None,
) -> NotificationIntent:
    return NotificationIntent(
        person_id=person_id,
        mission_id=mission_id,
        plan_id=f"{mission_id}:1",
        revision=1,
        notification_id=f"{mission_id}:{kind}",
        kind=kind,
        severity="warning",
        title=f"{person_id.replace('_', ' ').title()} – dags att gå",
        message="Lämna nu.",
        recommended_leave_time=datetime(2026, 10, 20, 5, 30, tzinfo=UTC),
        latest_leave_time=datetime(2026, 10, 20, 5, 35, tzinfo=UTC),
        quality="scheduled",
        reason_codes=(),
        packing_items=packing_items,
        channels=channels,
        tag=f"departure_{mission_id}",
        action_nonce=NONCE,
        lunch_menu=lunch_menu,
    )


def _mission(
    mission_id: str = MISSION_ID, *, status: str = "scheduled"
) -> MissionState:
    return MissionState(
        mission_id=mission_id,
        status=status,  # type: ignore[arg-type]
        departed_at=None,
        reopened=False,
        notified={},
        first_published_leave=None,
        action_nonce=NONCE,
    )


class ScriptRecorder:
    """Registers a stub ``script.turn_on`` and records every call."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail: set[str] = set()

        async def _turn_on(call: Any) -> ServiceResponse:
            entity = call.data.get("entity_id")
            self.calls.append(dict(call.data))
            if entity in self._fail:
                raise RuntimeError(f"channel {entity} boom")
            return None

        hass.services.async_register(
            "script", "turn_on", _turn_on, supports_response=SupportsResponse.OPTIONAL
        )

    def fail(self, entity_id: str) -> None:
        self._fail.add(entity_id)

    @property
    def entities(self) -> list[str]:
        return [c.get("entity_id") for c in self.calls]


@pytest.fixture
async def store(hass: HomeAssistant) -> FamilyDeparturesStore:
    s = FamilyDeparturesStore(hass, "entry-test")
    await s.async_load()
    return s


def _dispatcher(
    hass: HomeAssistant,
    store: FamilyDeparturesStore,
    profiles: dict[str, ProfileConfig],
    *,
    dry_run: bool = False,
    test_recipient: str | None = None,
    on_refresh: Any = None,
) -> NotificationDispatcher:
    return NotificationDispatcher(
        hass,
        store,
        lambda: profiles,
        dry_run=lambda: dry_run,
        test_recipient=lambda: test_recipient,
        request_refresh=on_refresh,
    )


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


async def test_push_intent_calls_configured_script_with_variables(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """An intent calls the profile's channel script with intent variables (§12.2)."""
    recorder = ScriptRecorder(hass)
    profile = _profile(scripts={"push": "script.push_kid_a"})
    dispatcher = _dispatcher(hass, store, {"kid_a": profile})

    await dispatcher.async_dispatch([_intent()])
    await hass.async_block_till_done()

    assert recorder.entities == ["script.push_kid_a"]
    variables = recorder.calls[0]["variables"]
    assert variables["person_id"] == "kid_a"
    assert variables["kind"] == "leave_now"
    assert variables["tag"] == f"departure_{MISSION_ID}"
    assert variables["action_nonce"] == NONCE
    # Times are serialised, not raw datetimes, and nothing is a secret.
    assert variables["recommended_leave_time"] == "2026-10-20T05:30:00+00:00"
    assert "url" not in variables and "dest_lat" not in variables


async def test_event_fired_with_intent_content(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """The hook event always carries the intent content (§12.2)."""
    ScriptRecorder(hass)
    events: list[Any] = []
    hass.bus.async_listen(EVENT_NOTIFICATION, lambda e: events.append(e.data))
    dispatcher = _dispatcher(hass, store, {"kid_a": _profile()})

    await dispatcher.async_dispatch([_intent()])
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0]["notification_id"] == f"{MISSION_ID}:leave_now"


async def test_script_variables_carry_the_lunch_menu(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    recorder = ScriptRecorder(hass)
    dispatcher = _dispatcher(hass, store, {"kid_a": _profile()})

    await dispatcher.async_dispatch(
        [_intent(lunch_menu="Lunch imorgon: Pasta"), _intent(kind="reminder")]
    )
    await hass.async_block_till_done()

    with_lunch, without = (call["variables"] for call in recorder.calls)
    assert with_lunch["lunch_menu"] == "Lunch imorgon: Pasta"
    assert without["lunch_menu"] is None


async def test_profile_without_channel_script_is_skipped_silently(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """A profile with no script for the channel is a no-op, not an error (§12.2)."""
    recorder = ScriptRecorder(hass)
    profile = _profile(scripts={})  # Kid A has no phone yet.
    dispatcher = _dispatcher(hass, store, {"kid_a": profile})

    await dispatcher.async_dispatch([_intent()])
    await hass.async_block_till_done()

    assert recorder.calls == []


async def test_dry_run_fires_event_but_calls_no_script(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """Global dry-run logs/fires the event but never calls scripts (§12.2)."""
    recorder = ScriptRecorder(hass)
    events: list[Any] = []
    hass.bus.async_listen(EVENT_NOTIFICATION, lambda e: events.append(e.data))
    dispatcher = _dispatcher(hass, store, {"kid_a": _profile()}, dry_run=True)

    await dispatcher.async_dispatch([_intent()])
    await hass.async_block_till_done()

    assert recorder.calls == []
    assert len(events) == 1


async def test_one_channel_failing_does_not_stop_the_others(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """A raising channel is isolated; the other channel still runs (§12.2)."""
    recorder = ScriptRecorder(hass)
    recorder.fail("script.push_kid_a")
    profile = _profile(
        scripts={"push": "script.push_kid_a", "family_chat": "script.chat"}
    )
    dispatcher = _dispatcher(hass, store, {"kid_a": profile})

    intent = _intent(channels=("push", "family_chat"))
    await dispatcher.async_dispatch([intent])
    await hass.async_block_till_done()

    assert "script.push_kid_a" in recorder.entities
    assert "script.chat" in recorder.entities


# ---------------------------------------------------------------------------
# Test-recipient mode (spec §12.2)
# ---------------------------------------------------------------------------


async def test_test_recipient_reroutes_all_profiles_to_one_script(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """All profiles' intents go only to the test script, with distinct tags."""
    recorder = ScriptRecorder(hass)
    kid_a = _profile("kid_a", scripts={"push": "script.push_kid_a"})
    parent_b = _profile("parent_b", scripts={"push": "script.push_parent_b"})
    dispatcher = _dispatcher(
        hass,
        store,
        {"kid_a": kid_a, "parent_b": parent_b},
        test_recipient="script.push_parent_a",
    )

    await dispatcher.async_dispatch(
        [
            _intent(person_id="kid_a", mission_id=MISSION_ID),
            _intent(person_id="parent_b", mission_id=PARENT_B_MISSION_ID),
        ]
    )
    await hass.async_block_till_done()

    # Only the test script is ever called; the profiles' own scripts are not.
    assert set(recorder.entities) == {"script.push_parent_a"}
    titles = [c["variables"]["title"] for c in recorder.calls]
    assert any(t.startswith("[TEST Kid A]") for t in titles)
    assert any(t.startswith("[TEST Parent B]") for t in titles)
    # Distinct per-person tags so the four do not replace each other.
    tags = {c["variables"]["tag"] for c in recorder.calls}
    assert tags == {
        f"departure_{MISSION_ID}_kid_a",
        f"departure_{PARENT_B_MISSION_ID}_parent_b",
    }
    # The action nonce stays the real mission's, so a tap confirms the right one.
    assert all(c["variables"]["action_nonce"] == NONCE for c in recorder.calls)


# ---------------------------------------------------------------------------
# Notification copies (feature 7, plan §6.4)
# ---------------------------------------------------------------------------


def _kid_and_parent(**kid: Any) -> dict[str, ProfileConfig]:
    return {
        "kid_a": _profile("kid_a", scripts={}, copy_to=("parent_b",), **kid),
        "parent_b": _profile("parent_b", scripts={"push": "script.push_parent_b"}),
    }


@freeze_time("2026-10-20T10:00:00+00:00")
async def test_a_kid_without_scripts_copies_to_the_parents_push_script(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    recorder = ScriptRecorder(hass)
    dispatcher = _dispatcher(hass, store, _kid_and_parent())

    await dispatcher.async_dispatch([_intent()])
    await hass.async_block_till_done()

    assert recorder.entities == ["script.push_parent_b"]
    variables = recorder.calls[0]["variables"]
    assert variables["title"] == "[Kopia] Kid A – dags att gå"
    assert variables["is_copy"] is True
    assert variables["copy_of_person_id"] == "kid_a"
    assert variables["copy_of_name"] == "Kid A"
    assert variables["recipient_person_id"] == "parent_b"
    # The tag and nonce are the original mission's.
    assert variables["tag"] == f"departure_{MISSION_ID}"
    assert variables["action_nonce"] == NONCE


@freeze_time("2026-10-20T10:00:00+00:00")
async def test_the_copy_follows_the_original_and_uses_the_push_script_only(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    recorder = ScriptRecorder(hass)
    profiles = {
        "kid_a": _profile(
            "kid_a",
            scripts={"push": "script.push_kid_a", "tts": "script.tts_kid_a"},
            copy_to=("parent_b",),
        ),
        "parent_b": _profile(
            "parent_b",
            scripts={"push": "script.push_parent_b", "tts": "script.tts_parent_b"},
        ),
    }
    dispatcher = _dispatcher(hass, store, profiles)

    await dispatcher.async_dispatch([_intent(channels=("push", "tts"))])
    await hass.async_block_till_done()

    # Original channels first, then the one copy; TTS is never duplicated.
    assert recorder.entities == [
        "script.push_kid_a",
        "script.tts_kid_a",
        "script.push_parent_b",
    ]


@freeze_time("2026-10-20T10:00:00+00:00")
async def test_mutual_copies_send_one_copy_each_way(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    recorder = ScriptRecorder(hass)
    profiles = {
        "kid_a": _profile("kid_a", scripts={}, copy_to=("parent_b",)),
        "parent_b": _profile(
            "parent_b", scripts={"push": "script.push_parent_b"}, copy_to=("kid_a",)
        ),
    }
    dispatcher = _dispatcher(hass, store, profiles)

    await dispatcher.async_dispatch([_intent()])
    await hass.async_block_till_done()

    assert recorder.entities == ["script.push_parent_b"]  # no copy back to kid_a


@freeze_time("2026-10-20T20:00:00+00:00")  # 22:00 local: parent_b is quiet
async def test_a_copy_in_the_recipients_quiet_hours_is_dropped_unless_critical(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    recorder = ScriptRecorder(hass)
    dispatcher = _dispatcher(hass, store, _kid_and_parent())

    await dispatcher.async_dispatch([_intent(kind="morning")])
    assert recorder.calls == []
    await dispatcher.async_dispatch([_intent(kind="critical")])
    await hass.async_block_till_done()

    assert recorder.entities == ["script.push_parent_b"]
    assert recorder.calls[0]["variables"]["kind"] == "critical"


@freeze_time("2026-10-20T10:00:00+00:00")
async def test_a_recipient_with_notifications_off_gets_no_copy(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    recorder = ScriptRecorder(hass)
    profiles = _kid_and_parent()
    profiles["parent_b"] = _profile(
        "parent_b",
        scripts={"push": "script.push_parent_b"},
        notifications_enabled=False,
    )
    dispatcher = _dispatcher(hass, store, profiles)

    await dispatcher.async_dispatch([_intent()])
    await hass.async_block_till_done()

    assert recorder.calls == []


@freeze_time("2026-10-20T10:00:00+00:00")
async def test_dry_run_sends_no_copy_but_the_event_lists_recipients(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    recorder = ScriptRecorder(hass)
    events: list[Any] = []
    hass.bus.async_listen(EVENT_NOTIFICATION, lambda e: events.append(e.data))
    dispatcher = _dispatcher(hass, store, _kid_and_parent(), dry_run=True)

    await dispatcher.async_dispatch([_intent()])
    await hass.async_block_till_done()

    assert recorder.calls == []
    assert [e["copied_to"] for e in events] == [["parent_b"]]


@freeze_time("2026-10-20T10:00:00+00:00")
async def test_test_recipient_mode_titles_and_tags_the_copy(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    recorder = ScriptRecorder(hass)
    dispatcher = _dispatcher(
        hass, store, _kid_and_parent(), test_recipient="script.push_test"
    )

    await dispatcher.async_dispatch([_intent()])
    await hass.async_block_till_done()

    assert set(recorder.entities) == {"script.push_test"}
    original, copy = [c["variables"] for c in recorder.calls]
    assert original["title"].startswith("[TEST Kid A]")
    assert copy["title"] == "[TEST Parent B ← Kid A] Kid A – dags att gå"
    assert copy["tag"] == f"departure_{MISSION_ID}_parent_b"
    assert copy["is_copy"] is True


@freeze_time("2026-10-20T10:00:00+00:00")
async def test_one_hook_event_per_original_listing_the_recipients(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    ScriptRecorder(hass)
    events: list[Any] = []
    hass.bus.async_listen(EVENT_NOTIFICATION, lambda e: events.append(e.data))
    dispatcher = _dispatcher(hass, store, _kid_and_parent())

    await dispatcher.async_dispatch([_intent()])
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0]["copied_to"] == ["parent_b"]
    assert events[0]["person_id"] == "kid_a"


# ---------------------------------------------------------------------------
# Companion-app actions (spec §12.3)
# ---------------------------------------------------------------------------


async def _fire_action(hass: HomeAssistant, action: str) -> None:
    hass.bus.async_fire(MOBILE_APP_ACTION_EVENT, {"action": action})
    await hass.async_block_till_done()


async def test_departed_action_marks_mission_departed(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """A valid FD_DEPARTED tap closes the mission (§12.3)."""
    store.set_mission(_mission())
    refreshed: list[bool] = []
    dispatcher = _dispatcher(
        hass,
        store,
        {"kid_a": _profile()},
        on_refresh=lambda: refreshed.append(True),
    )
    dispatcher.async_start()

    await _fire_action(hass, f"FD_DEPARTED|{MISSION_ID}|{NONCE}")

    state = store.get_mission(MISSION_ID)
    assert state is not None
    assert state.status == "departed"
    assert state.departed_at is not None
    assert refreshed == [True]

    dispatcher.async_shutdown()


async def test_off_action_writes_day_off_override(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """A valid FD_OFF tap writes an 'off' override for the mission's date (§12.3)."""
    store.set_mission(_mission())
    dispatcher = _dispatcher(hass, store, {"kid_a": _profile()})
    dispatcher.async_start()

    await _fire_action(hass, f"FD_OFF|{MISSION_ID}|{NONCE}")

    override = store.get_override("kid_a", TODAY)
    assert override is not None
    assert override.attendance == "off"

    dispatcher.async_shutdown()


async def test_skip_action_skips_only_that_mission(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """FD_SKIP closes the tapped mission and leaves the person's others alone."""
    extra_id = f"kid_a:{TODAY.isoformat()}:x-club-k1"
    store.set_mission(_mission())
    store.set_mission(_mission(extra_id))
    dispatcher = _dispatcher(hass, store, {"kid_a": _profile()})
    dispatcher.async_start()

    await _fire_action(hass, f"FD_SKIP|{extra_id}|{NONCE}")

    skipped = store.get_mission(extra_id)
    other = store.get_mission(MISSION_ID)
    assert skipped is not None and skipped.status == "skipped"
    assert other is not None and other.status == "scheduled"
    assert store.get_override("kid_a", TODAY) is None

    dispatcher.async_shutdown()


async def test_packed_action_acknowledges_that_dates_list(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """FD_PACKED acknowledges the notice's own date's packing list (§5.5, §12.3)."""
    # A PE lesson generates a "Gympakläder" item for TODAY.
    rules = (PackingRule(id="pe", match="IDRO", item="Gympakläder"),)
    profile = _profile(packing_rules=rules)
    calendar_id = profile.calendar_links[0].calendar_id
    store.set_mission(_mission())
    store.set_schedule(
        calendar_id,
        TODAY,
        ScheduleResult(
            status="ok",
            events=(
                ScheduleEvent(
                    uid="e1",
                    summary="Lektion IDRO1000X",
                    start=datetime(2026, 10, 20, 8, 20, tzinfo=UTC),
                    end=datetime(2026, 10, 20, 9, 10, tzinfo=UTC),
                    source_id=calendar_id,
                ),
            ),
            fetched_at=dt_util.utcnow(),
        ),
    )
    dispatcher = _dispatcher(hass, store, {"kid_a": profile})
    dispatcher.async_start()

    await _fire_action(hass, f"FD_PACKED|{MISSION_ID}|{NONCE}")

    assert "Gympakläder" in store.get_packing_acks("kid_a", TODAY)

    dispatcher.async_shutdown()


DIGEST_ID = f"kid_a:{TODAY.isoformat()}:day"


async def test_digest_packed_acknowledges_the_digests_date(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """FD_PACKED on the day-before digest acknowledges that day's list."""
    rules = (PackingRule(id="pe", match="IDRO", item="Gympakläder"),)
    profile = _profile(packing_rules=rules)
    calendar_id = profile.calendar_links[0].calendar_id
    store.set_mission(_mission(DIGEST_ID))
    store.set_schedule(
        calendar_id,
        TODAY,
        ScheduleResult(
            status="ok",
            events=(
                ScheduleEvent(
                    uid="e1",
                    summary="Lektion IDRO1000X",
                    start=datetime(2026, 10, 20, 8, 20, tzinfo=UTC),
                    end=datetime(2026, 10, 20, 9, 10, tzinfo=UTC),
                    source_id=calendar_id,
                ),
            ),
            fetched_at=dt_util.utcnow(),
        ),
    )
    dispatcher = _dispatcher(hass, store, {"kid_a": profile})
    dispatcher.async_start()

    await _fire_action(hass, f"FD_PACKED|{DIGEST_ID}|{NONCE}")

    assert "Gympakläder" in store.get_packing_acks("kid_a", TODAY)
    dispatcher.async_shutdown()


async def test_digest_off_closes_the_whole_day(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """FD_OFF on the digest writes the day's off override (all missions close)."""
    store.set_mission(_mission(DIGEST_ID))
    dispatcher = _dispatcher(hass, store, {"kid_a": _profile()})
    dispatcher.async_start()

    await _fire_action(hass, f"FD_OFF|{DIGEST_ID}|{NONCE}")

    override = store.get_override("kid_a", TODAY)
    assert override is not None and override.attendance == "off"
    dispatcher.async_shutdown()


async def test_digest_cannot_be_departed_or_skipped(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    store.set_mission(_mission(DIGEST_ID))
    dispatcher = _dispatcher(hass, store, {"kid_a": _profile()})
    dispatcher.async_start()

    await _fire_action(hass, f"FD_DEPARTED|{DIGEST_ID}|{NONCE}")
    await _fire_action(hass, f"FD_SKIP|{DIGEST_ID}|{NONCE}")

    state = store.get_mission(DIGEST_ID)
    assert state is not None and state.status == "scheduled"
    dispatcher.async_shutdown()


async def test_action_with_wrong_nonce_is_a_noop(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """A stale nonce (yesterday's notification) changes nothing (§12.3)."""
    store.set_mission(_mission())
    dispatcher = _dispatcher(hass, store, {"kid_a": _profile()})
    dispatcher.async_start()

    await _fire_action(hass, f"FD_DEPARTED|{MISSION_ID}|wrong-nonce")

    state = store.get_mission(MISSION_ID)
    assert state is not None
    assert state.status == "scheduled"

    dispatcher.async_shutdown()


async def test_action_for_unknown_mission_is_a_noop(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """An action for a mission with no state does nothing (§12.3)."""
    dispatcher = _dispatcher(hass, store, {"kid_a": _profile()})
    dispatcher.async_start()

    # No mission stored at all.
    await _fire_action(hass, f"FD_DEPARTED|{MISSION_ID}|{NONCE}")

    assert store.get_mission(MISSION_ID) is None

    dispatcher.async_shutdown()


async def test_foreign_action_id_is_ignored(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """Another integration's action id is ignored without error (§12.3)."""
    store.set_mission(_mission())
    dispatcher = _dispatcher(hass, store, {"kid_a": _profile()})
    dispatcher.async_start()

    await _fire_action(hass, "SOME_OTHER_APP_ACTION")
    await _fire_action(hass, "FD_UNKNOWN|x|y")

    state = store.get_mission(MISSION_ID)
    assert state is not None and state.status == "scheduled"

    dispatcher.async_shutdown()


async def test_shutdown_stops_listening_for_actions(
    hass: HomeAssistant, store: FamilyDeparturesStore
) -> None:
    """After shutdown a companion action no longer mutates state (§12.3)."""
    store.set_mission(_mission())
    dispatcher = _dispatcher(hass, store, {"kid_a": _profile()})
    dispatcher.async_start()
    dispatcher.async_shutdown()

    await _fire_action(hass, f"FD_DEPARTED|{MISSION_ID}|{NONCE}")

    state = store.get_mission(MISSION_ID)
    assert state is not None and state.status == "scheduled"


# ---------------------------------------------------------------------------
# Example scripts validate against the script config schema (acceptance)
# ---------------------------------------------------------------------------


async def test_example_scripts_yaml_validates(hass: HomeAssistant) -> None:
    """Every example channel script passes HA's script config schema."""
    from homeassistant.components.script.config import async_validate_config_item

    path = Path(__file__).parents[1] / "examples" / "scripts.yaml"
    text = await hass.async_add_executor_job(path.read_text, "utf-8")
    raw = yaml.safe_load(text)
    assert isinstance(raw, dict) and raw

    for object_id, config in raw.items():
        validated = await async_validate_config_item(hass, object_id, config)
        assert validated is not None
