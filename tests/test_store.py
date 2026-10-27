"""Tests for the persistent Store wrapper (spec §5.5, §8, §11.3, §15).

These exercise the behaviours a restart depends on: a full round-trip of every
record type, pruning of records older than the retention window, migration of
a hand-written version-1 payload, and graceful recovery from a corrupt file.
The ``hass_storage`` fixture stands in for the on-disk ``.storage`` layer, so
no real file I/O happens.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time

from custom_components.family_departures.const import DOMAIN
from custom_components.family_departures.models import (
    ArrivalRequirement,
    DayOverride,
    DeparturePlan,
    EventTags,
    MarginBreakdown,
    MissionState,
    NotifiedRecord,
    ScheduleEvent,
    ScheduleResult,
)
from custom_components.family_departures.store import (
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    FamilyDeparturesStore,
)
from homeassistant.core import HomeAssistant

ENTRY_ID = "entry123"
STORAGE_KEY = f"{DOMAIN}.{ENTRY_ID}"
# A fixed "now" so prune cutoffs are deterministic. Local date = 2026-10-20.
NOW = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)


def _sample_override(local_date: date = date(2026, 10, 20)) -> DayOverride:
    return DayOverride(
        person_id="kid_b",
        local_date=local_date,
        mode="car",
        attendance="normal",
        arrival_time=time(8, 15),
    )


def _sample_mission(mission_id: str = "kid_b:2026-10-20:morning") -> MissionState:
    return MissionState(
        mission_id=mission_id,
        status="scheduled",
        departed_at=None,
        reopened=False,
        notified={
            "evening": NotifiedRecord(
                kind="evening",
                sent_at=datetime(2026, 10, 19, 18, 0, tzinfo=UTC),
                leave_time=datetime(2026, 10, 20, 6, 15, tzinfo=UTC),
                revision=3,
            )
        },
        first_published_leave=datetime(2026, 10, 20, 6, 15, tzinfo=UTC),
        action_nonce="abc123",
    )


def _sample_schedule() -> ScheduleResult:
    event = ScheduleEvent(
        uid="evt-1",
        summary="Lektion IDRO1000X",
        start=datetime(2026, 10, 20, 6, 30, tzinfo=UTC),
        end=datetime(2026, 10, 20, 7, 30, tzinfo=UTC),
        source_id="kid_b_schoolsoft",
    )
    return ScheduleResult(
        status="ok",
        events=(event,),
        fetched_at=datetime(2026, 10, 20, 4, 0, tzinfo=UTC),
        source_modified_at=datetime(2026, 10, 19, 20, 0, tzinfo=UTC),
        content_hash="deadbeef",
        error_code=None,
        stale=False,
    )


def _sample_plan(mission_id: str = "kid_b:2026-10-20:morning") -> DeparturePlan:
    requirement = ArrivalRequirement(
        mission_id=mission_id,
        person_id="kid_b",
        local_date=date(2026, 10, 20),
        event_id="evt-1",
        event_start=datetime(2026, 10, 20, 6, 30, tzinfo=UTC),
        arrival_deadline=datetime(2026, 10, 20, 6, 25, tzinfo=UTC),
        destination_id="school",
        source="kid_b_schoolsoft",
    )
    return DeparturePlan(
        mission_id=mission_id,
        plan_id="plan-1",
        revision=3,
        requirement=requirement,
        mode="public_transport",
        recommended_leave=datetime(2026, 10, 20, 5, 55, tzinfo=UTC),
        latest_leave=datetime(2026, 10, 20, 6, 0, tzinfo=UTC),
        last_on_time_alternative_leave=datetime(2026, 10, 20, 5, 50, tzinfo=UTC),
        predicted_arrival=datetime(2026, 10, 20, 6, 24, tzinfo=UTC),
        journey_id="journey-9",
        route_summary="Buss 670",
        quality="scheduled",
        feasible=True,
        status="scheduled",
        breakdown=MarginBreakdown(
            travel=25, access_walk=5, boarding=3, departure=2, arrival=5, extra_after=0
        ),
        reason_codes=("selected_latest_safe",),
        config_revision=7,
    )


def _populate(store: FamilyDeparturesStore) -> None:
    store.data.overrides[("kid_b", date(2026, 10, 20))] = _sample_override()
    store.data.missions["kid_b:2026-10-20:morning"] = _sample_mission()
    store.data.packing_acks[("kid_b", date(2026, 10, 20))] = ("Gympakläder",)
    store.data.schedules[("kid_b_schoolsoft", date(2026, 10, 20))] = _sample_schedule()
    store.data.plans["kid_b:2026-10-20:morning"] = _sample_plan()


async def test_round_trip_all_records(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """Every record type survives a save/load cycle unchanged."""
    store = FamilyDeparturesStore(hass, ENTRY_ID)
    _populate(store)
    await store.async_save()

    reloaded = FamilyDeparturesStore(hass, ENTRY_ID)
    state = await reloaded.async_load(now=NOW)

    assert state.overrides[("kid_b", date(2026, 10, 20))] == _sample_override()
    assert state.missions["kid_b:2026-10-20:morning"] == _sample_mission()
    assert state.packing_acks[("kid_b", date(2026, 10, 20))] == ("Gympakläder",)
    assert state.schedules[("kid_b_schoolsoft", date(2026, 10, 20))] == (
        _sample_schedule()
    )
    assert state.plans["kid_b:2026-10-20:morning"] == _sample_plan()


async def test_debounced_save_persists(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """async_schedule_save writes through after the debounce flushes."""
    store = FamilyDeparturesStore(hass, ENTRY_ID)
    store.set_mission(_sample_mission())
    # Flush the debounced write deterministically.
    await store.async_save()

    assert STORAGE_KEY in hass_storage
    payload = hass_storage[STORAGE_KEY]
    assert isinstance(payload, dict)
    assert payload["version"] == STORAGE_VERSION
    assert "kid_b:2026-10-20:morning" in payload["data"]["missions"]


async def test_prune_removes_old_records(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """Missions, overrides and acks older than the retention window are dropped."""
    store = FamilyDeparturesStore(hass, ENTRY_ID)
    _populate(store)
    old_date = date(2026, 10, 1)  # 19 days before NOW's local date.
    store.data.overrides[("kid_b", old_date)] = _sample_override(old_date)
    store.data.packing_acks[("kid_b", old_date)] = ("Gammalt",)
    old_mission_id = "kid_b:2026-10-01:morning"
    store.data.missions[old_mission_id] = _sample_mission(old_mission_id)
    store.data.plans[old_mission_id] = _sample_plan(old_mission_id)
    await store.async_save()

    reloaded = FamilyDeparturesStore(hass, ENTRY_ID)
    state = await reloaded.async_load(now=NOW)

    assert ("kid_b", old_date) not in state.overrides
    assert ("kid_b", old_date) not in state.packing_acks
    assert old_mission_id not in state.missions
    # A plan for a pruned mission is dropped too.
    assert old_mission_id not in state.plans
    # Current-day records remain.
    assert ("kid_b", date(2026, 10, 20)) in state.overrides
    assert "kid_b:2026-10-20:morning" in state.missions


async def test_prune_trims_schedule_horizon(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """The schedule cache keeps only today and tomorrow (spec §15)."""
    store = FamilyDeparturesStore(hass, ENTRY_ID)
    today = date(2026, 10, 20)
    tomorrow = date(2026, 10, 21)
    yesterday = date(2026, 10, 19)
    day_after = date(2026, 10, 22)
    for d in (yesterday, today, tomorrow, day_after):
        store.data.schedules[("kid_b_schoolsoft", d)] = _sample_schedule()
    await store.async_save()

    reloaded = FamilyDeparturesStore(hass, ENTRY_ID)
    state = await reloaded.async_load(now=NOW)

    kept = {key[1] for key in state.schedules}
    assert kept == {today, tomorrow}


async def test_migration_from_v1_payload(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """A hand-written version-1 payload loads through the migration hook."""
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "overrides": {
                "kid_b|2026-10-20": {
                    "person_id": "kid_b",
                    "local_date": "2026-10-20",
                    "mode": "car",
                    "attendance": "normal",
                    "arrival_time": "08:15:00",
                }
            },
            "missions": {},
            "packing_acks": {},
            "schedules": {},
            "plans": {},
        },
    }

    store = FamilyDeparturesStore(hass, ENTRY_ID)
    state = await store.async_load(now=NOW)

    assert state.overrides[("kid_b", date(2026, 10, 20))] == _sample_override()


async def test_corrupt_file_yields_empty_state(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    caplog,
) -> None:
    """A corrupt payload logs a warning and starts empty instead of crashing."""
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        # ``data`` must be a mapping; a list is corrupt for our schema.
        "data": ["not", "a", "mapping"],
    }

    store = FamilyDeparturesStore(hass, ENTRY_ID)
    with caplog.at_level(logging.WARNING):
        state = await store.async_load(now=NOW)

    assert state.overrides == {}
    assert state.missions == {}
    assert "corrupt" in caplog.text.lower()


async def test_load_missing_store_is_empty(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """Loading when nothing was ever saved yields a clean empty state."""
    store = FamilyDeparturesStore(hass, ENTRY_ID)
    state = await store.async_load(now=NOW)

    assert state.overrides == {}
    assert state.missions == {}
    assert state.schedules == {}
    assert state.plans == {}


async def test_bad_single_record_does_not_wipe_rest(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """One malformed mission is dropped while the rest load fine."""
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "overrides": {},
            "missions": {
                "good:2026-10-20:morning": {
                    "mission_id": "good:2026-10-20:morning",
                    "status": "scheduled",
                    "departed_at": None,
                    "reopened": False,
                    "notified": {},
                    "first_published_leave": None,
                    "action_nonce": "n",
                },
                "bad:2026-10-20:morning": {
                    # Missing required mission_id/status -> dropped.
                    "reopened": False,
                },
            },
            "packing_acks": {},
            "schedules": {},
            "plans": {},
        },
    }

    store = FamilyDeparturesStore(hass, ENTRY_ID)
    state = await store.async_load(now=NOW)

    assert "good:2026-10-20:morning" in state.missions
    assert "bad:2026-10-20:morning" not in state.missions


async def test_minor_2_migration_drops_schedules_keeps_missions(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """The minor-2 bump drops the schedule cache but keeps the ledger (plan §2.3).

    The schedule cache was keyed per profile destination in v1.1 and cannot be
    re-keyed to calendar id, so it is dropped (it refetches). Missions (incl.
    the notification ledger), plans, overrides and packing acks are preserved,
    so the main mission's ``evening`` notice is not resent after the upgrade.
    """
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "overrides": {
                "kid_b|2026-10-20": {
                    "person_id": "kid_b",
                    "local_date": "2026-10-20",
                    "mode": "car",
                    "attendance": "normal",
                    "arrival_time": "08:15:00",
                }
            },
            "missions": {
                "kid_b:2026-10-20:morning": {
                    "mission_id": "kid_b:2026-10-20:morning",
                    "status": "scheduled",
                    "departed_at": None,
                    "reopened": False,
                    "notified": {
                        "evening": {
                            "kind": "evening",
                            "sent_at": "2026-10-19T18:00:00+00:00",
                            "leave_time": "2026-10-20T06:15:00+00:00",
                            "revision": 3,
                        }
                    },
                    "first_published_leave": "2026-10-20T06:15:00+00:00",
                    "action_nonce": "abc123",
                }
            },
            "packing_acks": {"kid_b|2026-10-20": ["Gympakläder"]},
            # Old per-destination schedule cache: must be dropped.
            "schedules": {
                "kid_b_destination|2026-10-20": {
                    "status": "ok",
                    "events": [],
                    "fetched_at": "2026-10-20T04:00:00+00:00",
                    "source_modified_at": None,
                    "content_hash": None,
                    "error_code": None,
                    "stale": False,
                }
            },
            "plans": {},
        },
    }

    store = FamilyDeparturesStore(hass, ENTRY_ID)
    state = await store.async_load(now=NOW)

    # Schedules dropped; ledger and the rest kept.
    assert state.schedules == {}
    mission = state.missions["kid_b:2026-10-20:morning"]
    assert "evening" in mission.notified
    assert state.overrides[("kid_b", date(2026, 10, 20))] == _sample_override()
    assert state.packing_acks[("kid_b", date(2026, 10, 20))] == ("Gympakläder",)


async def test_new_event_and_requirement_fields_round_trip(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """The v2 event tags/location and requirement slot/link fields survive a cycle."""
    event = ScheduleEvent(
        uid="evt-x",
        summary="Fotboll",
        start=datetime(2026, 10, 20, 15, 30, tzinfo=UTC),
        end=datetime(2026, 10, 20, 17, 0, tzinfo=UTC),
        source_id="shared_calendar",
        location="Sporthallen",
        tags=EventTags(location="Badhuset", transport="cykel", attendees=("Barn A",)),
        occurrence_key="abc123def4",
    )
    schedule = ScheduleResult(
        status="ok",
        events=(event,),
        fetched_at=datetime(2026, 10, 20, 4, 0, tzinfo=UTC),
    )
    requirement = ArrivalRequirement(
        mission_id="kid_b:2026-10-20:x-extra-abc123def4",
        person_id="kid_b",
        local_date=date(2026, 10, 20),
        event_id="evt-x",
        event_start=datetime(2026, 10, 20, 15, 30, tzinfo=UTC),
        arrival_deadline=datetime(2026, 10, 20, 15, 25, tzinfo=UTC),
        destination_id="badhuset",
        source="shared_calendar",
        slot="x-extra-abc123def4",
        link_id="extra",
        mode="static",
        label="Fotboll",
        destination_name="Badhuset",
        warnings=("home_gap_too_short",),
    )
    plan = DeparturePlan(
        mission_id=requirement.mission_id,
        plan_id="plan-x",
        revision=1,
        requirement=requirement,
        mode="static",
        recommended_leave=datetime(2026, 10, 20, 14, 55, tzinfo=UTC),
        latest_leave=datetime(2026, 10, 20, 15, 0, tzinfo=UTC),
        last_on_time_alternative_leave=None,
        predicted_arrival=datetime(2026, 10, 20, 15, 24, tzinfo=UTC),
        journey_id=None,
        route_summary=None,
        quality="scheduled",
        feasible=True,
        status="scheduled",
        breakdown=None,
        reason_codes=(),
        config_revision=1,
    )

    store = FamilyDeparturesStore(hass, ENTRY_ID)
    store.data.schedules[("shared_calendar", date(2026, 10, 20))] = schedule
    # A plan is only kept for a mission that still exists (prune rule).
    store.data.missions[requirement.mission_id] = MissionState(
        mission_id=requirement.mission_id,
        status="scheduled",
        departed_at=None,
        reopened=False,
        notified={},
        first_published_leave=None,
        action_nonce="n",
    )
    store.data.plans[requirement.mission_id] = plan
    await store.async_save()

    reloaded = FamilyDeparturesStore(hass, ENTRY_ID)
    state = await reloaded.async_load(now=NOW)

    loaded_event = state.schedules[("shared_calendar", date(2026, 10, 20))].events[0]
    assert loaded_event == event
    assert loaded_event.location == "Sporthallen"
    assert loaded_event.tags == EventTags(
        location="Badhuset", transport="cykel", attendees=("Barn A",)
    )
    assert loaded_event.occurrence_key == "abc123def4"

    loaded_req = state.plans[requirement.mission_id].requirement
    assert loaded_req == requirement
    assert loaded_req.slot == "x-extra-abc123def4"
    assert loaded_req.link_id == "extra"
    assert loaded_req.warnings == ("home_gap_too_short",)
    # The store is now at minor version 2.
    assert STORAGE_MINOR_VERSION == 2
