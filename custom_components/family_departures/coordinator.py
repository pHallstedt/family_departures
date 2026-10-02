"""Coordinator for the Family Departures integration (spec §3, §11.1, §11.2).

The coordinator is module 4 of the §3 architecture: it owns the asynchronous
I/O, the shared cache, bounded parallelism, polling cadence, recomputation and
publication. The planning math (``planner``), schedule selection (``schedule``),
packing (``packing``) and notification decisions (``notification_policy``) stay
pure and are only *called* from here.

What one update round does, per profile and isolated so one failure never
fails the others (spec §15):

1. Resolve the day's override (today) and the effective travel mode: today's
   override mode wins over the profile default (spec §4.3).
2. Fetch the schedule for the local day (ICS fetch I/O or ``calendar.get_events``)
   and select the arrival requirement, distinguishing an empty day from a
   source error (spec §5.3, §5.4). The previous fetch's events feed
   cancellation detection.
3. Build the packing list from the day's included events (spec §5.5).
4. Get the travel data for the effective mode and plan:
   * ``static`` makes no network calls at all (spec §6.4, §11.1).
   * ``car`` asks the shared Waze provider, falling back to the configured
     reserve minutes when Waze is unavailable (spec §6.3).
   * ``public_transport`` asks the SL journey provider (spec §6.1).
5. Persist the plan and mission state through the versioned Store and expose
   the per-profile result for the entity layer (T14).

A separate, lighter pass computes *tomorrow's* preliminary plan and stores it
as a preview without replacing today's published plan (spec §11.1).

Staleness guard (spec §11.2): every plan carries the ``config_revision`` that
was current when the round started. A result produced for an old revision is
dropped rather than overwriting a newer plan, so a slow API answer arriving
after a reconfigure cannot clobber fresh advice.

Concurrency (spec §11.2): at most two external calls run at once (a shared
semaphore), each profile holds its own lock for the duration of its round, and
shared routes are single-flighted inside the Waze provider's cache.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    DATA_HOME_LAT,
    DATA_HOME_LON,
    DATA_ICS_URLS,
    DEFAULT_CHANGE_THRESHOLD_MINUTES,
    DOMAIN,
    FETCH_FAILURE_THRESHOLD,
    ISSUE_INVALID_SOURCE,
    ISSUE_MISSING_DESTINATION,
    ISSUE_REPEATED_FETCH_FAILURE,
    OPT_PROFILES,
)
from .models import (
    ArrivalRequirement,
    DayOverride,
    DeparturePlan,
    DurationResult,
    JourneyResult,
    Margins,
    MissionState,
    Mode,
    PackingList,
    PackingRule,
    ProfileConfig,
    RequirementOutcome,
    ScheduleResult,
    SourceFilter,
)
from .packing import build_packing_list
from .planner import plan_fixed, plan_transit
from .providers.base import CarProvider, JourneyProvider, ScheduleProvider
from .providers.ha_calendar import HaCalendarScheduleProvider
from .providers.ics import IcsScheduleProvider
from .providers.sl_journey import SlJourneyProvider
from .providers.waze import WazeCarProvider
from .schedule import select_requirement
from .store import FamilyDeparturesStore
from .timeutil import local_date_of

_LOGGER = logging.getLogger(__name__)

# Default polling cadence (spec §11.1). The adaptive interval tightens inside
# the morning window and relaxes well before departure; this base value is the
# "more than 2 h before" cadence used until the first plan is known.
DEFAULT_UPDATE_INTERVAL = timedelta(minutes=15)
# Cadence 2 h–30 min before the recommended departure (spec §11.1: SL every
# 10 min).
NEAR_UPDATE_INTERVAL = timedelta(minutes=10)
# Cadence in the final 30 min before departure (spec §11.1: every 5 min).
IMMINENT_UPDATE_INTERVAL = timedelta(minutes=5)
# Relaxed cadence outside any active morning window (spec §11.1: evening/>2 h).
IDLE_UPDATE_INTERVAL = timedelta(minutes=30)

# Window boundaries relative to the recommended departure (spec §11.1).
NEAR_WINDOW = timedelta(hours=2)
IMMINENT_WINDOW = timedelta(minutes=30)

# At most two external calls run concurrently (spec §11.2).
MAX_CONCURRENT_EXTERNAL_CALLS = 2

# A mission's timers (and polling) stop at the latest 60 min after the event
# starts (spec §10); used to decide a round is no longer "active".
MISSION_TIMEOUT_MINUTES = 60


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


ScheduleProviderFactory = Callable[[ProfileConfig, str | None], ScheduleProvider]
JourneyProviderFactory = Callable[[], JourneyProvider]
CarProviderFactory = Callable[[], CarProvider]


@dataclass(slots=True)
class ProviderFactories:
    """Pluggable provider constructors so tests can inject fakes.

    The coordinator never imports a concrete provider directly in its logic; it
    goes through these factories. The defaults build the real adapters bound to
    the shared aiohttp session / HA service calls.
    """

    schedule: ScheduleProviderFactory
    journey: JourneyProviderFactory
    car: CarProviderFactory


def _default_factories(hass: HomeAssistant) -> ProviderFactories:
    """Build the production provider factories bound to ``hass``.

    The Waze and SL providers are constructed once and shared across profiles so
    their per-route cache and single-flight locking deduplicate calls for a
    route or stop used by more than one person (spec §11.2).
    """
    session = async_get_clientsession(hass)
    waze = WazeCarProvider(hass)
    sl = SlJourneyProvider(session)

    def make_schedule(profile: ProfileConfig, ics_url: str | None) -> ScheduleProvider:
        if profile.source_type == "ha_calendar":
            if not profile.calendar_entity_id:
                raise ValueError(
                    f"profile {profile.id} has no calendar_entity_id configured"
                )
            return HaCalendarScheduleProvider(
                hass,
                profile.calendar_entity_id,
                profile.source_filter,
                profile.destination_id,
            )
        if not ics_url:
            raise ValueError(f"profile {profile.id} has no ICS URL configured")
        return IcsScheduleProvider(
            session, ics_url, profile.source_filter, profile.destination_id
        )

    return ProviderFactories(
        schedule=make_schedule,
        journey=lambda: sl,
        car=lambda: waze,
    )


# ---------------------------------------------------------------------------
# Per-profile result published to the entity layer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProfileResult:
    """Everything the entity layer needs for one profile for *today*.

    Carrying the outcome and packing alongside the plan lets the dashboard tell
    "no schedule" from "source error" from "day off" (spec §5.4) without
    re-deriving anything, and show the packing list and quality markers.

    ``local_date``, ``schedule`` and ``previous_state`` are internal bookkeeping
    the coordinator uses to persist the round *after* the staleness guard (spec
    §11.2); the entity layer only reads ``profile``/``outcome``/``plan``/
    ``packing``.
    """

    profile: ProfileConfig
    outcome: RequirementOutcome
    plan: DeparturePlan | None
    packing: PackingList
    local_date: date
    schedule: ScheduleResult
    previous_state: MissionState | None = None
    tomorrow_plan: DeparturePlan | None = None


@dataclass(slots=True)
class SourceHealth:
    """Per-source health counters for diagnostics and repairs (spec §15).

    Tracked per ``destination_id`` (one schedule source per profile in v1). It
    holds only operational metadata - timestamps, an error *code* and counts -
    and never any schedule content, URL or coordinate, so it is safe to export
    verbatim in diagnostics (review-privacy-security).
    """

    last_success: datetime | None = None
    last_error_code: str | None = None
    last_error_at: datetime | None = None
    cache_hits: int = 0
    external_calls: int = 0
    consecutive_failures: int = 0

    def record_result(self, result: ScheduleResult, now: datetime) -> None:
        """Fold one fetch result into the counters.

        A reused cache hit (unchanged content hash → provider returns an ``ok``
        result flagged ``stale=False`` but produced without a network parse) is
        not something we can tell apart from a fresh ``ok`` here, so we only
        count the external call and the success/error. The provider owns the
        actual cache-hit signal; see :meth:`record_cache_hit`.
        """
        self.external_calls += 1
        if result.status == "error":
            self.last_error_code = result.error_code or "error"
            self.last_error_at = now
            self.consecutive_failures += 1
        else:
            self.last_success = now
            self.consecutive_failures = 0

    def record_cache_hit(self) -> None:
        """Count a reuse of the previous parse (unchanged feed, spec §15)."""
        self.cache_hits += 1

    def as_diagnostics(self) -> dict[str, Any]:
        """Operational snapshot for diagnostics (no content, safe to export)."""
        return {
            "last_success": self.last_success.isoformat()
            if self.last_success is not None
            else None,
            "last_error_code": self.last_error_code,
            "last_error_at": self.last_error_at.isoformat()
            if self.last_error_at is not None
            else None,
            "cache_hits": self.cache_hits,
            "external_calls": self.external_calls,
            "consecutive_failures": self.consecutive_failures,
        }


@dataclass(slots=True)
class _ProfileRuntime:
    """Mutable per-profile runtime kept across update rounds."""

    config: ProfileConfig
    ics_url: str | None
    schedule_provider: ScheduleProvider
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


CoordinatorData = dict[str, ProfileResult]


class FamilyDeparturesCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """One coordinator per config entry (spec §3, §11)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        store: FamilyDeparturesStore,
        *,
        factories: ProviderFactories | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )
        self._entry = entry
        self._store = store
        self._factories = factories or _default_factories(hass)
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTERNAL_CALLS)
        self._journey = self._factories.journey()
        self._car = self._factories.car()
        # The config revision bumps whenever the entry's options/data change so
        # a stale in-flight result can be discarded (spec §11.2).
        self._config_revision = 0
        self._runtimes: dict[str, _ProfileRuntime] = {}
        # Per-source operational health, keyed on destination_id, for the
        # diagnostics export and the repeated-failure repair (spec §15).
        self._health: dict[str, SourceHealth] = {}
        # Profile ids whose source could not be built (missing URL/calendar);
        # surfaced as an "invalid source" Repairs issue (spec §15).
        self._invalid_sources: dict[str, str] = {}
        self._load_profiles()

    # -- Configuration ------------------------------------------------------

    @property
    def config_revision(self) -> int:
        """The revision in force now; stamped onto every plan this round."""
        return self._config_revision

    def _load_profiles(self) -> None:
        """(Re)build per-profile runtime from the entry, bumping the revision.

        Called at construction and whenever the entry is reconfigured. A profile
        whose source cannot be built (e.g. an ICS profile with no URL yet) is
        skipped with a warning rather than failing the whole entry, so a
        profile can be half-configured while the others work (spec §15).
        """
        self._config_revision += 1
        home = self._home_coords()
        ics_urls = self._entry.data.get(DATA_ICS_URLS, {}) or {}
        raw_profiles = self._entry.options.get(OPT_PROFILES, {}) or {}

        runtimes: dict[str, _ProfileRuntime] = {}
        invalid_sources: dict[str, str] = {}
        for profile_id, raw in raw_profiles.items():
            if not isinstance(raw, Mapping):
                continue
            try:
                config = _profile_from_options(str(profile_id), raw)
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning(
                    "Skipping profile %s with invalid config: %s", profile_id, err
                )
                invalid_sources[str(profile_id)] = "invalid_config"
                continue
            ics_url = ics_urls.get(profile_id)
            try:
                provider = self._factories.schedule(config, ics_url)
            except (ValueError, TypeError) as err:
                _LOGGER.warning(
                    "Skipping profile %s without a usable source: %s",
                    profile_id,
                    err,
                )
                invalid_sources[config.id] = "no_source"
                continue
            runtimes[config.id] = _ProfileRuntime(
                config=config,
                ics_url=ics_url,
                schedule_provider=provider,
            )
        self._runtimes = runtimes
        self._invalid_sources = invalid_sources
        self._home = home

    def _home_coords(self) -> tuple[float, float]:
        """Return the household origin coordinates from ``entry.data``."""
        return (
            float(self._entry.data.get(DATA_HOME_LAT, 0.0)),
            float(self._entry.data.get(DATA_HOME_LON, 0.0)),
        )

    # -- Update cadence -----------------------------------------------------

    def _recommended_interval(self, data: CoordinatorData, now: datetime) -> timedelta:
        """Derive the next poll interval from the published plans (spec §11.1).

        The morning window is derived from the activity time and the plan, not
        from hard-coded clock times. The tightest interval any active plan needs
        wins; a day with only ``static`` plans (no network) or no active mission
        relaxes to the idle cadence.
        """
        tightest: timedelta | None = None
        for result in data.values():
            plan = result.plan
            if plan is None or plan.recommended_leave is None:
                continue
            if plan.mode == "static":
                # Static plans need no network refresh (spec §11.1).
                continue
            if plan.status in ("departed", "skipped"):
                continue
            # Past the timeout the mission's polling stops (spec §10, §11.1).
            if now >= plan.requirement.event_start + timedelta(
                minutes=MISSION_TIMEOUT_MINUTES
            ):
                continue
            until = plan.recommended_leave - now
            if until <= IMMINENT_WINDOW:
                candidate = IMMINENT_UPDATE_INTERVAL
            elif until <= NEAR_WINDOW:
                candidate = NEAR_UPDATE_INTERVAL
            else:
                candidate = DEFAULT_UPDATE_INTERVAL
            if tightest is None or candidate < tightest:
                tightest = candidate
        return tightest or IDLE_UPDATE_INTERVAL

    # -- Update round -------------------------------------------------------

    async def _async_update_data(self) -> CoordinatorData:
        """Run one update round over all profiles (spec §11)."""
        now = dt_util.utcnow()
        revision = self._config_revision
        today = local_date_of(now)

        results = await asyncio.gather(
            *(
                self._process_profile(runtime, today, now, revision)
                for runtime in self._runtimes.values()
            )
        )
        data: CoordinatorData = {
            result.profile.id: result for result in results if result is not None
        }

        # A result produced for an older revision must never overwrite a newer
        # plan (spec §11.2); if the config changed mid-round, drop this round's
        # plans *and* skip persisting them, keeping what we already published.
        if revision != self._config_revision:
            _LOGGER.debug(
                "Config changed during update (%s -> %s); discarding stale round",
                revision,
                self._config_revision,
            )
            return self.data or {}

        # Persist only after the staleness guard passes, so a slow round for an
        # old revision cannot write plans/missions over the new configuration.
        self._persist_round(data, now)
        self._reconcile_issues(data)
        self.update_interval = self._recommended_interval(data, now)
        return data

    def _reconcile_issues(self, data: CoordinatorData) -> None:
        """Create/clear Repairs issues from this round's state (spec §15).

        Three conditions are surfaced, each scoped to one profile so one broken
        source never hides another:

        * invalid source - a profile whose schedule source could not be built
          (missing ICS URL or calendar entity);
        * missing destination - a profile with no destination coordinates set;
        * repeated fetch failure - a source that failed to fetch at least
          :data:`FETCH_FAILURE_THRESHOLD` times in a row.

        Issues are cleared again the moment the condition no longer holds.
        """
        hass = self.hass

        # Invalid source: present for exactly the currently unbuildable profiles.
        for profile_id, reason in self._invalid_sources.items():
            ir.async_create_issue(
                hass,
                DOMAIN,
                f"{ISSUE_INVALID_SOURCE}_{profile_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_INVALID_SOURCE,
                translation_placeholders={"profile": profile_id, "reason": reason},
            )
        for runtime in self._runtimes.values():
            ir.async_delete_issue(
                hass, DOMAIN, f"{ISSUE_INVALID_SOURCE}_{runtime.config.id}"
            )

        # Missing destination and repeated fetch failures, per built profile.
        for runtime in self._runtimes.values():
            config = runtime.config
            missing_dest_id = f"{ISSUE_MISSING_DESTINATION}_{config.id}"
            if _is_missing_destination(config):
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    missing_dest_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key=ISSUE_MISSING_DESTINATION,
                    translation_placeholders={"profile": config.id},
                )
            else:
                ir.async_delete_issue(hass, DOMAIN, missing_dest_id)

            health = self._health.get(config.destination_id)
            failure_id = f"{ISSUE_REPEATED_FETCH_FAILURE}_{config.id}"
            if (
                health is not None
                and health.consecutive_failures >= FETCH_FAILURE_THRESHOLD
            ):
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    failure_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_REPEATED_FETCH_FAILURE,
                    translation_placeholders={
                        "profile": config.id,
                        "failures": str(health.consecutive_failures),
                        "error_code": health.last_error_code or "unknown",
                    },
                )
            else:
                ir.async_delete_issue(hass, DOMAIN, failure_id)

    def _persist_round(self, data: CoordinatorData, now: datetime) -> None:
        """Write the round's schedules, plans and mission state (spec §11.3).

        Persistence is deferred to here (after the revision guard) so a stale
        round never mutates the store. One debounced save flushes all writes.
        """
        for result in data.values():
            config = result.profile
            self._store.set_schedule(
                config.destination_id, result.local_date, result.schedule
            )
            if result.plan is not None:
                self._persist_mission(result.plan, result.previous_state, now)
                self._store.set_plan(result.plan)
        self._store.async_schedule_save()

    async def _process_profile(
        self,
        runtime: _ProfileRuntime,
        today: date,
        now: datetime,
        revision: int,
    ) -> ProfileResult | None:
        """Process one profile in isolation (spec §15: others survive a failure)."""
        try:
            async with runtime.lock:
                return await self._plan_profile(runtime, today, now, revision)
        except Exception:  # noqa: BLE001 - isolate one profile's failure
            _LOGGER.exception(
                "Failed to update profile %s; other profiles continue",
                runtime.config.id,
            )
            return None

    async def _plan_profile(
        self,
        runtime: _ProfileRuntime,
        today: date,
        now: datetime,
        revision: int,
    ) -> ProfileResult:
        """Fetch, select, pack, plan and persist today's mission for a profile."""
        config = runtime.config
        override = self._store.get_override(config.id, today)

        # Effective mode: today's override wins over the default (spec §4.3).
        mode = _effective_mode(config, override)

        # 1. Schedule fetch + requirement selection. The schedule is persisted
        #    later by ``_persist_round`` (after the staleness guard), so the
        #    ``previous`` read here is last round's cache for cancellation
        #    detection (spec §5.2).
        previous = self._store.get_schedule(config.destination_id, today)
        previous_events = previous.events if previous is not None else None
        result = await self._fetch_schedule(runtime, today, now)
        self._record_health(config.destination_id, result, now)

        # The shared household holiday calendar is not configured in v1, so no
        # automatic "Ledig" day is inferred here; an explicit day-off override
        # still closes the mission (spec §5.4). Wiring a holiday source is a
        # later task.
        holiday = False
        state = self._store.get_mission(_mission_id_for(config.id, today))
        outcome = select_requirement(
            config,
            today,
            result,
            override,
            holiday,
            previous_events,
            state,
            now,
        )

        packing = build_packing_list(
            config.id,
            today,
            result.events,
            config.packing_rules,
            override,
            self._store.get_packing_acks(config.id, today),
        )

        if outcome.requirement is None:
            # No mission today: nothing to plan, but the outcome/packing still
            # drive the dashboard texts (spec §5.4).
            return ProfileResult(
                profile=config,
                outcome=outcome,
                plan=None,
                packing=packing,
                local_date=today,
                schedule=result,
                previous_state=state,
            )

        # 2. Travel data + plan for the effective mode. Persistence is deferred
        #    to ``_persist_round`` so a stale round cannot write to the store.
        plan = await self._plan_for_mode(
            runtime, mode, outcome.requirement, state, now, revision
        )
        plan = replace(plan, config_revision=revision)

        return ProfileResult(
            profile=config,
            outcome=outcome,
            plan=plan,
            packing=packing,
            local_date=today,
            schedule=result,
            previous_state=state,
        )

    async def _fetch_schedule(
        self, runtime: _ProfileRuntime, d: date, now: datetime
    ) -> ScheduleResult:
        """Fetch one day's schedule, bounded by the external-call semaphore.

        The per-source health counters are updated here so diagnostics and the
        repeated-failure repair have fresh figures (spec §15). Only today's
        fetch feeds the counters; the tomorrow-preview pass passes
        ``track_health=False`` so a preview failure does not raise a repair for
        a feed whose *today* fetch is fine.
        """
        async with self._semaphore:
            result = await runtime.schedule_provider.async_get_day(d)
        return result

    def _record_health(
        self, destination_id: str, result: ScheduleResult, now: datetime
    ) -> None:
        """Fold a today-fetch result into the source health counters."""
        health = self._health.setdefault(destination_id, SourceHealth())
        health.record_result(result, now)

    def source_health(self) -> dict[str, SourceHealth]:
        """Expose the per-source health map for diagnostics (T19)."""
        return self._health

    @property
    def invalid_sources(self) -> dict[str, str]:
        """Profile ids whose source could not be built (for repairs/diagnostics)."""
        return dict(self._invalid_sources)

    async def _plan_for_mode(
        self,
        runtime: _ProfileRuntime,
        mode: Mode,
        req: ArrivalRequirement,
        state: MissionState | None,
        now: datetime,
        revision: int,
    ) -> DeparturePlan:
        """Dispatch to the right planner for ``mode`` (spec §6)."""
        config = runtime.config
        if mode == "public_transport":
            journey = await self._fetch_journey(req, config, now)
            previous_plan = self._store.get_plan(req.mission_id)
            return plan_transit(
                req, journey, config, state, previous_plan, now, revision
            )

        if mode == "static":
            # Static door-to-door duration; no network (spec §6.4, §11.1).
            duration = self._static_duration(config, now)
            return plan_fixed(req, "static", duration, config, state, now, revision)

        # Car: Waze with the configured reserve-minutes fallback (spec §6.3).
        duration = await self._fetch_car_duration(config, now)
        return plan_fixed(req, "car", duration, config, state, now, revision)

    async def _fetch_journey(
        self, req: ArrivalRequirement, config: ProfileConfig, now: datetime
    ) -> JourneyResult:
        """Query the SL journey planner for the mission's deadline (spec §6.1)."""
        async with self._semaphore:
            return await self._journey.async_plan(
                self._home,
                (config.dest_lat, config.dest_lon),
                req.arrival_deadline,
                now,
            )

    async def _fetch_car_duration(
        self, config: ProfileConfig, now: datetime
    ) -> DurationResult:
        """Get a Waze car duration, falling back to reserve minutes (spec §6.3)."""
        async with self._semaphore:
            duration = await self._car.async_get_duration(
                self._home,
                (config.dest_lat, config.dest_lon),
                True,
                None,
            )
        if duration.minutes is not None:
            return duration
        if config.car_fallback_minutes is not None:
            # Marked ``estimated`` so the plan carries a clear quality downgrade
            # instead of a silent live value (spec §6.3).
            return DurationResult(
                minutes=float(config.car_fallback_minutes),
                fetched_at=now,
                source="fallback",
                quality="estimated",
            )
        return duration

    def _static_duration(self, config: ProfileConfig, now: datetime) -> DurationResult:
        """Build the fixed static duration from the profile (spec §6.4)."""
        minutes = (
            float(config.static_minutes) if config.static_minutes is not None else None
        )
        return DurationResult(
            minutes=minutes,
            fetched_at=now,
            source="static",
            quality="scheduled" if minutes is not None else "unavailable",
            route_name=config.static_label,
        )

    def _persist_mission(
        self, plan: DeparturePlan, state: MissionState | None, now: datetime
    ) -> None:
        """Record the mission state behind a plan (spec §8, §11.3).

        A new mission gets a fresh action nonce; an existing one keeps its
        confirmed ``departed``/``skipped`` status and nonce so a later recompute
        never resets a confirmed departure (spec §11.2). The notification ledger
        is owned by the scheduler (T17) and left untouched here.
        """
        if state is None:
            new_state = MissionState(
                mission_id=plan.mission_id,
                status=plan.status,
                departed_at=None,
                reopened=False,
                notified={},
                first_published_leave=plan.recommended_leave,
                action_nonce=secrets.token_hex(8),
            )
            self._store.set_mission(new_state)
            return

        if state.status in ("departed", "skipped"):
            # Keep the confirmed terminal state; do not downgrade to the clock.
            return

        first_leave = state.first_published_leave or plan.recommended_leave
        self._store.set_mission(
            replace(
                state,
                status=plan.status,
                first_published_leave=first_leave,
            )
        )

    # -- Tomorrow preview ---------------------------------------------------

    async def async_update_tomorrow_preview(self) -> dict[str, DeparturePlan]:
        """Compute tomorrow's preliminary plans without touching today (spec §11.1).

        The preview is stored under tomorrow's mission ids so the dashboard can
        show "tomorrow" separately; it never replaces today's published plan or
        drives today's sensors.
        """
        now = dt_util.utcnow()
        revision = self._config_revision
        tomorrow = local_date_of(now) + timedelta(days=1)

        previews: dict[str, DeparturePlan] = {}
        for runtime in self._runtimes.values():
            try:
                async with runtime.lock:
                    plan = await self._preview_profile(runtime, tomorrow, now, revision)
            except Exception:  # noqa: BLE001 - one preview failing is non-fatal
                _LOGGER.exception(
                    "Failed to compute tomorrow preview for %s", runtime.config.id
                )
                continue
            if plan is not None:
                previews[plan.mission_id] = plan
                if revision == self._config_revision:
                    self._store.set_plan(plan)
        return previews

    async def _preview_profile(
        self,
        runtime: _ProfileRuntime,
        d: date,
        now: datetime,
        revision: int,
    ) -> DeparturePlan | None:
        """Build tomorrow's preliminary plan for one profile, or ``None``."""
        config = runtime.config
        override = self._store.get_override(config.id, d)
        mode = _effective_mode(config, override)

        result = await self._fetch_schedule(runtime, d, now)
        self._store.set_schedule(config.destination_id, d, result)
        outcome = select_requirement(
            config, d, result, override, False, None, None, now
        )
        if outcome.requirement is None:
            return None
        plan = await self._plan_for_mode(
            runtime, mode, outcome.requirement, None, now, revision
        )
        return replace(plan, config_revision=revision)

    # -- Reconfiguration hook ----------------------------------------------

    def async_reload_config(self) -> None:
        """Rebuild profiles after the entry's options/data changed (spec §4.1).

        Bumps the config revision so any in-flight round's results are dropped
        rather than overwriting plans built from the new configuration.
        """
        self._load_profiles()


# ---------------------------------------------------------------------------
# Option deserialisation
# ---------------------------------------------------------------------------


def _is_missing_destination(config: ProfileConfig) -> bool:
    """A profile needs destination coordinates for every non-static mode.

    The options flow writes 0.0/0.0 when no location was picked; treat that
    unset origin as missing (spec §15: "saknad destination"). Static mode uses
    a fixed duration and does not need coordinates.
    """
    if config.default_mode == "static":
        return False
    return config.dest_lat == 0.0 and config.dest_lon == 0.0


def _effective_mode(config: ProfileConfig, override: DayOverride | None) -> Mode:
    """Return the mode for a day: a day override's mode wins (spec §4.3)."""
    if override is not None and override.mode is not None:
        return override.mode
    return config.default_mode


def _mission_id_for(person_id: str, d: date) -> str:
    """Local import-free mission id (keeps this module's call sites explicit)."""
    from .timeutil import make_mission_id

    return make_mission_id(person_id, d)


def _parse_time_value(value: Any, default: str) -> Any:
    from datetime import time as _time

    if isinstance(value, _time):
        return value
    try:
        return _time.fromisoformat(str(value))
    except ValueError:
        return _time.fromisoformat(default)


def _profile_from_options(profile_id: str, raw: Mapping[str, Any]) -> ProfileConfig:
    """Build a :class:`ProfileConfig` from one stored options profile.

    Mirrors the shape written by the options flow (T12); the secret ICS URL is
    deliberately *not* read here (it lives in ``entry.data`` and reaches the
    coordinator through the provider factory, review-privacy-security).
    """
    source_filter = SourceFilter(
        exclude_patterns=tuple(raw.get("exclude_patterns", ()) or ()),
        include_patterns=tuple(raw.get("include_patterns", ()) or ()),
    )
    packing_rules = tuple(
        PackingRule(
            id=str(rule["id"]),
            match=str(rule["match"]),
            item=str(rule["item"]),
        )
        for rule in raw.get("packing_rules", []) or []
        if isinstance(rule, Mapping)
    )
    margins = Margins(
        arrival=int(raw["arrival"]),
        departure=int(raw["departure"]),
        boarding=int(raw["boarding"]),
        parking_and_walk=int(raw["parking_and_walk"]),
        min_transfer=int(raw["min_transfer"]),
    )
    static_minutes = raw.get("static_minutes")
    car_fallback_minutes = raw.get("car_fallback_minutes")
    return ProfileConfig(
        id=profile_id,
        name=str(raw["name"]),
        source_type=raw["source_type"],
        calendar_entity_id=raw.get("calendar_entity_id"),
        source_filter=source_filter,
        destination_id=str(raw.get("destination_id", f"{profile_id}_destination")),
        dest_lat=float(raw["dest_lat"]),
        dest_lon=float(raw["dest_lon"]),
        default_mode=raw["default_mode"],
        static_minutes=int(static_minutes) if static_minutes is not None else None,
        static_label=raw.get("static_label"),
        weather_adjust=bool(raw.get("weather_adjust", False)),
        car_fallback_minutes=(
            int(car_fallback_minutes) if car_fallback_minutes is not None else None
        ),
        margins=margins,
        weekday_mask=frozenset(int(day) for day in raw.get("weekday_mask", ()) or ()),
        packing_rules=packing_rules,
        person_entity_id=raw.get("person_entity_id"),
        notifications_enabled=bool(raw.get("notifications_enabled", False)),
        change_threshold_minutes=int(
            raw.get("change_threshold_minutes", DEFAULT_CHANGE_THRESHOLD_MINUTES)
        ),
        quiet_start=_parse_time_value(raw.get("quiet_start"), "22:00:00"),
        quiet_end=_parse_time_value(raw.get("quiet_end"), "06:00:00"),
        scripts=dict(raw.get("scripts", {}) or {}),
        evening_notice_enabled=bool(raw.get("evening_notice_enabled", True)),
    )


__all__ = [
    "CoordinatorData",
    "FamilyDeparturesCoordinator",
    "ProfileResult",
    "ProviderFactories",
    "SourceHealth",
]
