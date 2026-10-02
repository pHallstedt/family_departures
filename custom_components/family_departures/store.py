"""Persistent state for the Family Departures integration (spec §11.3, §15).

A single versioned :class:`homeassistant.helpers.storage.Store` per config
entry holds everything that must survive a restart:

* per-``(person_id, local_date)`` day overrides;
* per-``mission_id`` :class:`MissionState` (including the notification ledger);
* per-``(person_id, local_date)`` packing acknowledgements;
* the last fetched schedule per source for *today and tomorrow only* (hash plus
  normalised events), so an unchanged feed need not be reparsed (spec §15);
* the last published :class:`DeparturePlan` per mission, so a restart can
  reason about what was already shown.

Design rules honoured here:

* Saves are debounced (spec §11.3): callers mutate the in-memory
  :class:`StoredState` and call :meth:`FamilyDeparturesStore.async_schedule_save`.
* On load, missions, acks and overrides whose local date is older than seven
  days are pruned, and the schedule cache is trimmed to a two-day horizon
  (spec §15 keeps the schedule cache small).
* A corrupted or unreadable store yields an empty state and a logged warning
  rather than crashing the entry.
* A migration hook from version 1 exists for future schema changes.

Serialisation keeps the horizon small and never writes secrets: only
normalised event summaries/times are stored, never ICS URLs or tokens, which
live in ``entry.data`` and are read by the coordinator alone (spec §15).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .models import (
    ArrivalRequirement,
    DayOverride,
    DeparturePlan,
    MarginBreakdown,
    MissionState,
    NotifiedRecord,
    ScheduleEvent,
    ScheduleResult,
)
from .timeutil import local_date_of

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION: Final = 1
STORAGE_MINOR_VERSION: Final = 1
# Keep finished missions, overrides and acknowledgements for one week (spec §15).
RETENTION_DAYS: Final = 7
# Debounce window for state writes (spec §11.3: no write per minute per profile).
SAVE_DELAY: Final = 10.0


def _storage_key(entry_id: str) -> str:
    """Return the per-entry Store key."""
    return f"{DOMAIN}.{entry_id}"


@dataclass(slots=True)
class StoredState:
    """In-memory view of everything persisted for one config entry.

    Mutable on purpose: the coordinator and scheduler update it in place and
    trigger a debounced save. Keys are tuples/strings that serialise to stable
    JSON strings so the on-disk layout is deterministic.
    """

    overrides: dict[tuple[str, date], DayOverride] = field(default_factory=dict)
    missions: dict[str, MissionState] = field(default_factory=dict)
    packing_acks: dict[tuple[str, date], tuple[str, ...]] = field(default_factory=dict)
    # Last schedule result per (source_id, local_date), today/tomorrow only.
    schedules: dict[tuple[str, date], ScheduleResult] = field(default_factory=dict)
    # Last published plan per mission_id.
    plans: dict[str, DeparturePlan] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _dt_to_json(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt_from_json(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    return dt_util.parse_datetime(value)


def _date_to_json(value: date) -> str:
    return value.isoformat()


def _date_from_json(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _time_to_json(value: time | None) -> str | None:
    return value.isoformat() if value is not None else None


def _time_from_json(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


def _override_to_json(override: DayOverride) -> dict[str, Any]:
    return {
        "person_id": override.person_id,
        "local_date": _date_to_json(override.local_date),
        "mode": override.mode,
        "attendance": override.attendance,
        "arrival_time": _time_to_json(override.arrival_time),
    }


def _override_from_json(data: Mapping[str, Any]) -> DayOverride:
    local_date = _date_from_json(data.get("local_date"))
    if local_date is None:
        raise ValueError("override missing local_date")
    return DayOverride(
        person_id=str(data["person_id"]),
        local_date=local_date,
        mode=data.get("mode"),
        attendance=data.get("attendance"),
        arrival_time=_time_from_json(data.get("arrival_time")),
    )


def _notified_to_json(record: NotifiedRecord) -> dict[str, Any]:
    return {
        "kind": record.kind,
        "sent_at": _dt_to_json(record.sent_at),
        "leave_time": _dt_to_json(record.leave_time),
        "revision": record.revision,
    }


def _notified_from_json(data: Mapping[str, Any]) -> NotifiedRecord:
    sent_at = _dt_from_json(data.get("sent_at"))
    if sent_at is None:
        raise ValueError("notified record missing sent_at")
    return NotifiedRecord(
        kind=str(data["kind"]),
        sent_at=sent_at,
        leave_time=_dt_from_json(data.get("leave_time")),
        revision=int(data["revision"]),
    )


def _mission_to_json(state: MissionState) -> dict[str, Any]:
    return {
        "mission_id": state.mission_id,
        "status": state.status,
        "departed_at": _dt_to_json(state.departed_at),
        "reopened": state.reopened,
        "notified": {
            kind: _notified_to_json(record) for kind, record in state.notified.items()
        },
        "first_published_leave": _dt_to_json(state.first_published_leave),
        "action_nonce": state.action_nonce,
    }


def _mission_from_json(data: Mapping[str, Any]) -> MissionState:
    raw_notified = data.get("notified") or {}
    notified = {
        str(kind): _notified_from_json(record)
        for kind, record in raw_notified.items()
        if isinstance(record, Mapping)
    }
    return MissionState(
        mission_id=str(data["mission_id"]),
        status=data["status"],
        departed_at=_dt_from_json(data.get("departed_at")),
        reopened=bool(data.get("reopened", False)),
        notified=notified,
        first_published_leave=_dt_from_json(data.get("first_published_leave")),
        action_nonce=str(data.get("action_nonce", "")),
    )


def _event_to_json(event: ScheduleEvent) -> dict[str, Any]:
    return {
        "uid": event.uid,
        "summary": event.summary,
        "start": _dt_to_json(event.start),
        "end": _dt_to_json(event.end),
        "source_id": event.source_id,
    }


def _event_from_json(data: Mapping[str, Any]) -> ScheduleEvent:
    start = _dt_from_json(data.get("start"))
    end = _dt_from_json(data.get("end"))
    if start is None or end is None:
        raise ValueError("event missing start/end")
    return ScheduleEvent(
        uid=str(data["uid"]),
        summary=str(data["summary"]),
        start=start,
        end=end,
        source_id=str(data["source_id"]),
    )


def _schedule_to_json(result: ScheduleResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "events": [_event_to_json(event) for event in result.events],
        "fetched_at": _dt_to_json(result.fetched_at),
        "source_modified_at": _dt_to_json(result.source_modified_at),
        "content_hash": result.content_hash,
        "error_code": result.error_code,
        "stale": result.stale,
    }


def _schedule_from_json(data: Mapping[str, Any]) -> ScheduleResult:
    fetched_at = _dt_from_json(data.get("fetched_at"))
    if fetched_at is None:
        raise ValueError("schedule missing fetched_at")
    raw_events = data.get("events") or []
    events = tuple(
        _event_from_json(event) for event in raw_events if isinstance(event, Mapping)
    )
    return ScheduleResult(
        status=data["status"],
        events=events,
        fetched_at=fetched_at,
        source_modified_at=_dt_from_json(data.get("source_modified_at")),
        content_hash=data.get("content_hash"),
        error_code=data.get("error_code"),
        stale=bool(data.get("stale", False)),
    )


def _requirement_to_json(req: ArrivalRequirement) -> dict[str, Any]:
    return {
        "mission_id": req.mission_id,
        "person_id": req.person_id,
        "local_date": _date_to_json(req.local_date),
        "event_id": req.event_id,
        "event_start": _dt_to_json(req.event_start),
        "arrival_deadline": _dt_to_json(req.arrival_deadline),
        "destination_id": req.destination_id,
        "source": req.source,
    }


def _requirement_from_json(data: Mapping[str, Any]) -> ArrivalRequirement:
    local_date = _date_from_json(data.get("local_date"))
    event_start = _dt_from_json(data.get("event_start"))
    arrival_deadline = _dt_from_json(data.get("arrival_deadline"))
    if local_date is None or event_start is None or arrival_deadline is None:
        raise ValueError("requirement missing date fields")
    return ArrivalRequirement(
        mission_id=str(data["mission_id"]),
        person_id=str(data["person_id"]),
        local_date=local_date,
        event_id=str(data["event_id"]),
        event_start=event_start,
        arrival_deadline=arrival_deadline,
        destination_id=str(data["destination_id"]),
        source=str(data["source"]),
    )


def _breakdown_to_json(breakdown: MarginBreakdown | None) -> dict[str, Any] | None:
    if breakdown is None:
        return None
    return {
        "travel": breakdown.travel,
        "access_walk": breakdown.access_walk,
        "boarding": breakdown.boarding,
        "departure": breakdown.departure,
        "arrival": breakdown.arrival,
        "extra_after": breakdown.extra_after,
    }


def _breakdown_from_json(data: Any) -> MarginBreakdown | None:
    if not isinstance(data, Mapping):
        return None
    return MarginBreakdown(
        travel=int(data["travel"]),
        access_walk=int(data["access_walk"]),
        boarding=int(data["boarding"]),
        departure=int(data["departure"]),
        arrival=int(data["arrival"]),
        extra_after=int(data["extra_after"]),
    )


def _plan_to_json(plan: DeparturePlan) -> dict[str, Any]:
    return {
        "mission_id": plan.mission_id,
        "plan_id": plan.plan_id,
        "revision": plan.revision,
        "requirement": _requirement_to_json(plan.requirement),
        "mode": plan.mode,
        "recommended_leave": _dt_to_json(plan.recommended_leave),
        "latest_leave": _dt_to_json(plan.latest_leave),
        "last_on_time_alternative_leave": _dt_to_json(
            plan.last_on_time_alternative_leave
        ),
        "predicted_arrival": _dt_to_json(plan.predicted_arrival),
        "journey_id": plan.journey_id,
        "route_summary": plan.route_summary,
        "quality": plan.quality,
        "feasible": plan.feasible,
        "status": plan.status,
        "breakdown": _breakdown_to_json(plan.breakdown),
        "reason_codes": list(plan.reason_codes),
        "config_revision": plan.config_revision,
    }


def _plan_from_json(data: Mapping[str, Any]) -> DeparturePlan:
    requirement = _requirement_from_json(data["requirement"])
    reason_codes = tuple(str(code) for code in data.get("reason_codes") or ())
    return DeparturePlan(
        mission_id=str(data["mission_id"]),
        plan_id=str(data["plan_id"]),
        revision=int(data["revision"]),
        requirement=requirement,
        mode=data["mode"],
        recommended_leave=_dt_from_json(data.get("recommended_leave")),
        latest_leave=_dt_from_json(data.get("latest_leave")),
        last_on_time_alternative_leave=_dt_from_json(
            data.get("last_on_time_alternative_leave")
        ),
        predicted_arrival=_dt_from_json(data.get("predicted_arrival")),
        journey_id=data.get("journey_id"),
        route_summary=data.get("route_summary"),
        quality=data["quality"],
        feasible=bool(data["feasible"]),
        status=data["status"],
        breakdown=_breakdown_from_json(data.get("breakdown")),
        reason_codes=reason_codes,
        config_revision=int(data["config_revision"]),
    )


def _person_date_key(person_id: str, local_date: date) -> str:
    return f"{person_id}|{local_date.isoformat()}"


def _source_date_key(source_id: str, local_date: date) -> str:
    return f"{source_id}|{local_date.isoformat()}"


def _split_person_date(raw: str) -> tuple[str, date] | None:
    person_id, _, date_part = raw.rpartition("|")
    if not person_id:
        return None
    parsed = _date_from_json(date_part)
    if parsed is None:
        return None
    return person_id, parsed


def _state_to_json(state: StoredState) -> dict[str, Any]:
    """Serialise the in-memory state to a JSON-safe dict."""
    return {
        "overrides": {
            _person_date_key(person_id, local_date): _override_to_json(override)
            for (person_id, local_date), override in state.overrides.items()
        },
        "missions": {
            mission_id: _mission_to_json(mission)
            for mission_id, mission in state.missions.items()
        },
        "packing_acks": {
            _person_date_key(person_id, local_date): list(items)
            for (person_id, local_date), items in state.packing_acks.items()
        },
        "schedules": {
            _source_date_key(source_id, local_date): _schedule_to_json(result)
            for (source_id, local_date), result in state.schedules.items()
        },
        "plans": {
            mission_id: _plan_to_json(plan) for mission_id, plan in state.plans.items()
        },
    }


def _state_from_json(data: Mapping[str, Any]) -> StoredState:
    """Rebuild the in-memory state, skipping records that fail to parse.

    A single malformed record is dropped (and logged at debug) rather than
    failing the whole load, so one bad entry cannot wipe the rest.
    """
    state = StoredState()

    for raw_key, raw in (data.get("overrides") or {}).items():
        key = _split_person_date(str(raw_key))
        if key is None or not isinstance(raw, Mapping):
            continue
        try:
            state.overrides[key] = _override_from_json(raw)
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.debug("Skipping bad override %s: %s", raw_key, err)

    for mission_id, raw in (data.get("missions") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        try:
            state.missions[str(mission_id)] = _mission_from_json(raw)
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.debug("Skipping bad mission %s: %s", mission_id, err)

    for raw_key, raw in (data.get("packing_acks") or {}).items():
        key = _split_person_date(str(raw_key))
        if key is None or not isinstance(raw, list):
            continue
        state.packing_acks[key] = tuple(str(item) for item in raw)

    for raw_key, raw in (data.get("schedules") or {}).items():
        key = _split_person_date(str(raw_key))
        if key is None or not isinstance(raw, Mapping):
            continue
        try:
            state.schedules[key] = _schedule_from_json(raw)
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.debug("Skipping bad schedule %s: %s", raw_key, err)

    for mission_id, raw in (data.get("plans") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        try:
            state.plans[str(mission_id)] = _plan_from_json(raw)
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.debug("Skipping bad plan %s: %s", mission_id, err)

    return state


def _prune(state: StoredState, now: datetime) -> None:
    """Drop stale records in place (spec §15 retention and small horizon).

    Missions, overrides and packing acknowledgements whose local date is older
    than :data:`RETENTION_DAYS` are removed. The schedule cache is trimmed to
    today and tomorrow only; plans are kept only for missions still present.
    """
    today = local_date_of(now)
    cutoff = today - timedelta(days=RETENTION_DAYS)
    horizon_end = today + timedelta(days=1)

    state.overrides = {
        key: value for key, value in state.overrides.items() if key[1] >= cutoff
    }
    state.packing_acks = {
        key: value for key, value in state.packing_acks.items() if key[1] >= cutoff
    }
    state.schedules = {
        key: value
        for key, value in state.schedules.items()
        if today <= key[1] <= horizon_end
    }

    kept_missions: dict[str, MissionState] = {}
    for mission_id, mission in state.missions.items():
        mission_date = _mission_date(mission_id)
        if mission_date is None or mission_date >= cutoff:
            kept_missions[mission_id] = mission
    state.missions = kept_missions
    state.plans = {
        mission_id: plan
        for mission_id, plan in state.plans.items()
        if mission_id in state.missions
    }


def _mission_date(mission_id: str) -> date | None:
    """Extract the local date from a ``person:YYYY-MM-DD:slot`` mission id."""
    parts = mission_id.split(":")
    if len(parts) < 2:
        return None
    return _date_from_json(parts[1])


class FamilyDeparturesStore:
    """Versioned, debounced persistence wrapper around a HA ``Store``.

    Callers read and mutate :attr:`data` (a :class:`StoredState`) and then call
    :meth:`async_schedule_save` to persist with a debounce. :meth:`async_load`
    rebuilds the state, prunes stale records and never raises on a corrupt
    file.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store[dict[str, Any]] = _MigratingStore(
            hass,
            STORAGE_VERSION,
            _storage_key(entry_id),
            minor_version=STORAGE_MINOR_VERSION,
        )
        self.data = StoredState()

    async def async_load(self, *, now: datetime | None = None) -> StoredState:
        """Load persisted state, prune stale records and return it.

        A corrupt or unreadable store logs a warning and yields an empty state
        instead of crashing the config entry (spec §15 robustness).
        """
        when = now or dt_util.utcnow()
        try:
            raw = await self._store.async_load()
        except (ValueError, KeyError, TypeError) as err:
            _LOGGER.warning(
                "Could not read stored state (%s); starting empty",
                type(err).__name__,
            )
            self.data = StoredState()
            return self.data

        if not raw:
            self.data = StoredState()
            return self.data

        if not isinstance(raw, Mapping):
            _LOGGER.warning("Stored state was corrupt (not a mapping); starting empty")
            self.data = StoredState()
            return self.data

        try:
            state = _state_from_json(raw)
        except (ValueError, KeyError, TypeError, AttributeError) as err:
            _LOGGER.warning(
                "Stored state was corrupt (%s); starting empty",
                type(err).__name__,
            )
            self.data = StoredState()
            return self.data

        _prune(state, when)
        self.data = state
        return self.data

    def async_schedule_save(self) -> None:
        """Debounced persist of the current in-memory state (spec §11.3)."""
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    async def async_save(self) -> None:
        """Persist immediately (used on unload to flush pending changes)."""
        await self._store.async_save(self._data_to_save())

    def _data_to_save(self) -> dict[str, Any]:
        return _state_to_json(self.data)

    # Convenience accessors -------------------------------------------------

    def get_override(self, person_id: str, local_date: date) -> DayOverride | None:
        return self.data.overrides.get((person_id, local_date))

    def set_override(self, override: DayOverride) -> None:
        self.data.overrides[(override.person_id, override.local_date)] = override
        self.async_schedule_save()

    def get_mission(self, mission_id: str) -> MissionState | None:
        return self.data.missions.get(mission_id)

    def set_mission(self, mission: MissionState) -> None:
        self.data.missions[mission.mission_id] = mission
        self.async_schedule_save()

    def get_packing_acks(self, person_id: str, local_date: date) -> tuple[str, ...]:
        return self.data.packing_acks.get((person_id, local_date), ())

    def set_packing_acks(
        self, person_id: str, local_date: date, items: tuple[str, ...]
    ) -> None:
        self.data.packing_acks[(person_id, local_date)] = items
        self.async_schedule_save()

    def get_schedule(self, source_id: str, local_date: date) -> ScheduleResult | None:
        return self.data.schedules.get((source_id, local_date))

    def set_schedule(
        self, source_id: str, local_date: date, result: ScheduleResult
    ) -> None:
        self.data.schedules[(source_id, local_date)] = result
        self.async_schedule_save()

    def get_plan(self, mission_id: str) -> DeparturePlan | None:
        return self.data.plans.get(mission_id)

    def set_plan(self, plan: DeparturePlan) -> None:
        self.data.plans[plan.mission_id] = plan
        self.async_schedule_save()


class _MigratingStore(Store[dict[str, Any]]):
    """``Store`` subclass with a migration hook from version 1 (spec §11.3)."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Migrate stored data to the current schema.

        Version 1 is the first released schema, so there is nothing to migrate
        yet. The hook exists so a future bump has a home; unknown future
        versions fall through to the current layout unchanged.
        """
        if old_major_version > STORAGE_VERSION:
            # Downgrade: keep what we can; the loader drops unknown records.
            return old_data
        return old_data


__all__ = [
    "FamilyDeparturesStore",
    "StoredState",
    "STORAGE_VERSION",
    "STORAGE_MINOR_VERSION",
    "RETENTION_DAYS",
]
