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

From :data:`TOMORROW_PREVIEW_START` (18:00 local) each round also computes
*tomorrow's* schedule, packing list and preliminary plan and attaches them to
the profile's result as a preview, without replacing today's published plan
(spec §5.5, §11.1). The scheduler builds the 20:00 day-before digest from it.

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
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .calendar_links import filter_for_link
from .const import (
    DATA_HOME_LAT,
    DATA_HOME_LON,
    DATA_ICS_URLS,
    DOMAIN,
    FETCH_FAILURE_THRESHOLD,
    ISSUE_INVALID_SOURCE,
    ISSUE_MISSING_DESTINATION,
    ISSUE_PROFILE_WITHOUT_PERSON,
    ISSUE_REPEATED_FETCH_FAILURE,
    OPT_PROFILES,
)
from .household import parse_household
from .missions import (
    CONFLICT_WARNINGS,
    DayMissions,
    annotate_conflicts,
    select_missions,
)
from .models import (
    ArrivalRequirement,
    CalendarLink,
    CalendarSource,
    DayOverride,
    DeparturePlan,
    DurationResult,
    HouseholdConfig,
    JourneyResult,
    Location,
    LunchMenu,
    MissionState,
    Mode,
    PackingList,
    ProfileConfig,
    RequirementOutcome,
    ScheduleEvent,
    ScheduleResult,
    SourceFilter,
)
from .packing import build_packing_list
from .planner import plan_fixed, plan_transit
from .providers.base import (
    CarProvider,
    JourneyProvider,
    LunchProvider,
    ScheduleProvider,
)
from .providers.ha_calendar import HaCalendarScheduleProvider
from .providers.ics import IcsScheduleProvider
from .providers.lunch_calendar import HaCalendarLunchProvider
from .providers.sl_journey import SlJourneyProvider
from .providers.waze import WazeCarProvider
from .store import FamilyDeparturesStore
from .timeutil import TZ, local_date_of, make_mission_id

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

# From this local time every round also computes tomorrow's preliminary plan
# and packing list (spec §5.5, §11.1). The coordinator polls at least every
# 30 min (``IDLE_UPDATE_INTERVAL``), so several rounds run before the 20:00
# evening notice and its timer is always armed in time.
TOMORROW_PREVIEW_START = time(18, 0)

# Extra-link missions whose event is further away than the horizon are planned
# at most once per interval (the stored plan is reused in between) so SL and
# Waze volume stays bounded with many activities (plan item 6).
REPLAN_HORIZON = timedelta(hours=4)
REPLAN_INTERVAL = timedelta(minutes=30)
MAIN_SLOT = "morning"
# The day-before digest mission (``person:d:day``), see the notification policy.
DIGEST_SLOT = "day"


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


ScheduleProviderFactory = Callable[[CalendarSource, str | None], ScheduleProvider]
LunchProviderFactory = Callable[[str], LunchProvider]

# How long a fetched menu is reused: a good one for 3 h, a failure for 30 min
# (plan §7.2). Two profiles on one lunch entity share the entry.
LUNCH_OK_TTL = timedelta(hours=3)
LUNCH_ERROR_TTL = timedelta(minutes=30)
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
    lunch: LunchProviderFactory | None = None


def _default_factories(hass: HomeAssistant) -> ProviderFactories:
    """Build the production provider factories bound to ``hass``.

    The Waze and SL providers are constructed once and shared across profiles so
    their per-route cache and single-flight locking deduplicate calls for a
    route or stop used by more than one person (spec §11.2).
    """
    session = async_get_clientsession(hass)
    waze = WazeCarProvider(hass)
    sl = SlJourneyProvider(session)

    def make_schedule(
        calendar: CalendarSource, ics_url: str | None
    ) -> ScheduleProvider:
        # Providers filter nothing: per-link filtering happens downstream so one
        # fetch serves every profile linking the calendar (plan §4.1). The
        # provider's ``source_id`` is the calendar id, which keys the cache.
        if calendar.source_type == "ha_calendar":
            if not calendar.calendar_entity_id:
                raise ValueError(
                    f"calendar {calendar.id} has no calendar_entity_id configured"
                )
            return HaCalendarScheduleProvider(
                hass,
                calendar.calendar_entity_id,
                SourceFilter(),
                calendar.id,
            )
        if not ics_url:
            raise ValueError(f"calendar {calendar.id} has no ICS URL configured")
        return IcsScheduleProvider(session, ics_url, SourceFilter(), calendar.id)

    return ProviderFactories(
        schedule=make_schedule,
        journey=lambda: sl,
        car=lambda: waze,
        lunch=lambda entity_id: HaCalendarLunchProvider(hass, entity_id),
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

    The ``tomorrow_*`` fields carry tomorrow's preliminary preview. They are
    only set from :data:`TOMORROW_PREVIEW_START` local time and never drive
    today's sensors (spec §11.1). The scheduler builds the 20:00 day-before
    digest from ``tomorrow_missions`` and ``tomorrow_packing``;
    ``tomorrow_plan`` is the main mission's preview, and
    ``packing_list_tomorrow`` reads ``tomorrow_packing`` (spec §5.5).
    """

    profile: ProfileConfig
    outcome: RequirementOutcome
    plan: DeparturePlan | None
    packing: PackingList
    local_date: date
    schedule: ScheduleResult
    calendar_id: str | None = None
    previous_state: MissionState | None = None
    tomorrow_plan: DeparturePlan | None = None
    tomorrow_packing: PackingList | None = None
    tomorrow_schedule: ScheduleResult | None = None
    tomorrow_state: MissionState | None = None
    missions: tuple[MissionResult, ...] = ()
    tomorrow_missions: tuple[MissionResult, ...] = ()
    failed_extra_links: tuple[str, ...] = ()
    lunch_tomorrow: LunchMenu | None = None


@dataclass(frozen=True, slots=True)
class MissionResult:
    """One planned mission of a profile day (main ``morning`` or an extra event).

    ``plan.requirement`` carries any conflict warnings (plan §4.6). ``event`` is
    the calendar event behind the mission, when it could be identified.
    """

    link_id: str
    plan: DeparturePlan
    event: ScheduleEvent | None = None

    @property
    def requirement(self) -> ArrivalRequirement:
        return self.plan.requirement


@dataclass(frozen=True, slots=True)
class _DayPlan:
    """One profile day: aggregated missions, packing and the main raw schedule."""

    day: DayMissions
    missions: tuple[MissionResult, ...]
    packing: PackingList
    main_result: ScheduleResult
    main_state: MissionState | None


@dataclass(frozen=True, slots=True)
class _TomorrowPreview:
    """Tomorrow's schedule, packing list and preliminary plans for one profile."""

    schedule: ScheduleResult
    packing: PackingList
    plan: DeparturePlan | None
    state: MissionState | None
    missions: tuple[MissionResult, ...] = ()


@dataclass(slots=True)
class SourceHealth:
    """Per-calendar health counters for diagnostics and repairs (spec §15).

    Tracked per ``calendar_id`` - one record per shared calendar, fetched once
    per round however many profiles link it (plan §4.1, F5). It holds only
    operational metadata - timestamps, an error *code* and counts - and never
    any schedule content, URL or coordinate, so it is safe to export verbatim
    in diagnostics (review-privacy-security). ``linked_profile_count`` is the
    number of profiles linking the calendar, so a shared calendar shows once
    with its fan-out rather than per profile (plan §4.5, §8).
    """

    last_success: datetime | None = None
    last_error_code: str | None = None
    last_error_at: datetime | None = None
    cache_hits: int = 0
    external_calls: int = 0
    consecutive_failures: int = 0
    linked_profile_count: int = 0

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
            "linked_profile_count": self.linked_profile_count,
        }


@dataclass(slots=True)
class _ProfileRuntime:
    """Mutable per-profile runtime kept across update rounds.

    ``calendar_id`` is the profile's main-link calendar (the schedule-cache key
    for step 2); ``schedule_provider`` is the provider for that calendar, shared
    with any other profile linking the same calendar. ``destination`` is the
    resolved default :class:`Location`.
    """

    config: ProfileConfig
    calendar_id: str | None
    schedule_provider: ScheduleProvider | None
    destination: Location | None
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
        # Schedule providers shared per calendar id (one fetch per calendar).
        self._calendar_providers: dict[str, ScheduleProvider] = {}
        # Per-calendar operational health, keyed on calendar_id, for the
        # diagnostics export and the repeated-failure repair (spec §15).
        self._health: dict[str, SourceHealth] = {}
        # Profile ids whose source could not be built (missing URL/calendar);
        # surfaced as an "invalid source" Repairs issue (spec §15). Kept for
        # diagnostics; the Repairs issue itself is now keyed per calendar.
        self._invalid_sources: dict[str, str] = {}
        # Calendars whose provider could not be built (missing ICS URL or
        # calendar entity), keyed by calendar id -> reason. These drive the
        # per-calendar ``invalid_source`` Repairs issue (plan §4.1).
        self._invalid_calendars: dict[str, str] = {}
        # Number of profiles linking each calendar, for the shared-calendar
        # diagnostics (``linked_profile_count``) and repair placeholders.
        self._linked_profile_count: dict[str, int] = {}
        # Profile ids ever seen, so old per-profile issue ids from the v1 scheme
        # can be deleted once the per-calendar scheme takes over (plan §4.1).
        self._known_profile_ids: set[str] = set()
        # Calendar ids seen so far, so issues and health for one that was deleted
        # from the configuration can be cleared.
        self._seen_calendar_ids: set[str] = set()
        # When each extra mission was last planned, for the re-plan throttle.
        self._last_planned: dict[str, datetime] = {}
        # Tomorrow's menu per (lunch entity, date), with the time it was fetched.
        self._lunch_cache: dict[tuple[str, date], LunchMenu] = {}
        # Profiles are processed concurrently; the lock makes a shared entity one call.
        self._lunch_lock = asyncio.Lock()
        self._household: HouseholdConfig = HouseholdConfig(
            home=(0.0, 0.0), locations={}, calendars={}, profiles={}
        )
        self._load_profiles()

    # -- Configuration ------------------------------------------------------

    @property
    def config_revision(self) -> int:
        """The revision in force now; stamped onto every plan this round."""
        return self._config_revision

    @property
    def profile_configs(self) -> dict[str, ProfileConfig]:
        """Every configured, buildable profile keyed by id.

        Unlike ``data`` this includes a profile whose last round failed, so the
        entity platforms create its entities (shown unavailable until a round
        succeeds) and the dispatcher can still resolve its scripts (spec §15).
        """
        return {pid: runtime.config for pid, runtime in self._runtimes.items()}

    def _load_profiles(self) -> None:
        """(Re)build per-profile runtime from the entry, bumping the revision.

        Called at construction and whenever the entry is reconfigured. A profile
        whose source cannot be built (e.g. an ICS profile with no URL yet) is
        skipped with a warning rather than failing the whole entry, so a
        profile can be half-configured while the others work (spec §15).
        Such a profile intentionally gets no entities; it is surfaced as an
        ``invalid_source`` Repairs issue instead.
        """
        self._config_revision += 1
        ics_urls = self._entry.data.get(DATA_ICS_URLS, {}) or {}
        raw_profiles = self._entry.options.get(OPT_PROFILES, {}) or {}

        try:
            household = parse_household(self._entry.data, self._entry.options)
        except (KeyError, ValueError, TypeError) as err:
            # ``parse_household`` isolates per-profile failures itself; this is a
            # safety net for the household-level parse (home/locations/calendars).
            # Log only the exception type, never its message, which can embed a
            # coordinate value (§10, matching the config_flow paths).
            _LOGGER.warning("Could not parse household config: %s", type(err).__name__)
            household = HouseholdConfig(
                home=self._home_coords(), locations={}, calendars={}, profiles={}
            )
        self._household = household

        # Build one schedule provider per calendar (shared by its links, §4.1).
        calendar_providers: dict[str, ScheduleProvider] = {}
        invalid_calendars: dict[str, str] = {}
        for calendar_id, calendar in household.calendars.items():
            ics_url = ics_urls.get(calendar_id)
            try:
                calendar_providers[calendar_id] = self._factories.schedule(
                    calendar, ics_url
                )
            except ValueError, TypeError:
                # Log the calendar id (a slug) only, never the exception message
                # which can embed the URL value (§10, review-privacy-security).
                _LOGGER.warning(
                    "Skipping calendar %s without a usable source", calendar_id
                )
                invalid_calendars[calendar_id] = "no_source"

        # Count how many profiles link each calendar so a shared calendar is
        # reported once with its fan-out (plan §4.5, §8). Every link counts, not
        # just the main one, so the figure matches the real fetch fan-out.
        linked_profile_count: dict[str, int] = {}
        for config in household.profiles.values():
            seen: set[str] = set()
            for link in config.calendar_links:
                if link.calendar_id in seen:
                    continue
                seen.add(link.calendar_id)
                linked_profile_count[link.calendar_id] = (
                    linked_profile_count.get(link.calendar_id, 0) + 1
                )

        runtimes: dict[str, _ProfileRuntime] = {}
        invalid_sources: dict[str, str] = {}
        # A profile whose options failed to parse into a ProfileConfig: flag it.
        for profile_id, raw in raw_profiles.items():
            if not isinstance(raw, Mapping):
                continue
            if str(profile_id) not in household.profiles:
                _LOGGER.warning("Skipping profile %s with invalid config", profile_id)
                invalid_sources[str(profile_id)] = "invalid_config"

        for profile_id, config in household.profiles.items():
            main_link = config.main_link
            main_calendar_id = main_link.calendar_id if main_link is not None else None
            provider = (
                calendar_providers.get(main_calendar_id)
                if main_calendar_id is not None
                else None
            )
            if main_link is not None and provider is None:
                _LOGGER.warning(
                    "Skipping profile %s without a usable source", profile_id
                )
                invalid_sources[config.id] = "no_source"
                continue
            runtimes[config.id] = _ProfileRuntime(
                config=config,
                calendar_id=main_calendar_id,
                schedule_provider=provider,
                destination=household.locations.get(config.default_location_id),
            )
        self._runtimes = runtimes
        self._calendar_providers = calendar_providers
        self._invalid_sources = invalid_sources
        self._invalid_calendars = invalid_calendars
        self._linked_profile_count = linked_profile_count
        self._known_profile_ids.update(household.profiles)
        self._known_profile_ids.update(invalid_sources)
        # Seed/refresh the linked-profile count on each calendar's health record
        # so diagnostics show it even before the first fetch.
        for calendar_id, count in linked_profile_count.items():
            self._health.setdefault(
                calendar_id, SourceHealth()
            ).linked_profile_count = count
        self._household = household
        self._home = household.home

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
        plans = [
            plan
            for result in data.values()
            for plan in (
                [m.plan for m in result.missions] if result.missions else [result.plan]
            )
            if plan is not None
        ]
        for plan in plans:
            if plan.recommended_leave is None:
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
        """Run one update round in two phases (spec §11, plan §4.1).

        Phase 1 fetches every ``(calendar_id, day)`` pair that any profile needs
        exactly once - today for every linked calendar, plus tomorrow from
        :data:`TOMORROW_PREVIEW_START` - and folds the result into the
        per-calendar health. A calendar shared by two profiles is therefore
        fetched once per day, not once per profile (F5).

        Phase 2 processes each profile in isolation against that shared,
        pre-fetched map (spec §15: one profile failing never fails the others).
        """
        now = dt_util.utcnow()
        revision = self._config_revision
        today = local_date_of(now)

        # Phase 1: shared fetch of every needed calendar day, once each.
        fetched, failed = await self._fetch_all_calendar_days(today, now)

        # Phase 2: per-profile planning against the shared results.
        results = await asyncio.gather(
            *(
                self._process_profile(runtime, today, now, revision, fetched, failed)
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
        self._persist_round(data, now, fetched)
        self._reconcile_issues(data)
        self.update_interval = self._recommended_interval(data, now)
        return data

    def _needed_calendar_days(
        self, today: date, now: datetime
    ) -> set[tuple[str, date]]:
        """The set of ``(calendar_id, day)`` pairs this round needs (plan §4.1).

        Every calendar any profile links is fetched for *today*; from
        :data:`TOMORROW_PREVIEW_START` local time tomorrow is added too, so the
        evening preview and the 20:00 notice have their data. Using a set
        collapses a calendar shared by several profiles to one fetch per day.
        """
        want_tomorrow = now.astimezone(TZ).time() >= TOMORROW_PREVIEW_START
        days = [today] + ([today + timedelta(days=1)] if want_tomorrow else [])
        pairs: set[tuple[str, date]] = set()
        for config in self._runtimes.values():
            for link in config.config.calendar_links:
                if link.calendar_id not in self._calendar_providers:
                    continue
                for day in days:
                    pairs.add((link.calendar_id, day))
        return pairs

    async def _fetch_all_calendar_days(
        self, today: date, now: datetime
    ) -> tuple[dict[tuple[str, date], ScheduleResult], set[str]]:
        """Fetch every needed calendar day once, recording per-calendar health.

        The fetches run under the shared external-call semaphore (spec §11.2);
        ``asyncio.gather`` keeps them bounded by it. Only *today*'s fetch feeds
        the repeated-failure health counters so a failing tomorrow-preview fetch
        for an otherwise healthy feed does not raise a repair (plan §4.1).

        Returns the ``(calendar_id, day) -> result`` map plus the set of
        calendar ids whose *today* fetch raised. A raised fetch (an unexpected
        provider crash, not a handled ``error`` result) is kept distinct from an
        empty day: the profiles whose main calendar it is are failed in phase 2
        so their entities go unavailable rather than silently reading an empty
        day as "nothing registered" (spec §5.4, §15).
        """
        pairs = sorted(self._needed_calendar_days(today, now))

        async def _one(
            calendar_id: str, day: date
        ) -> tuple[tuple[str, date], ScheduleResult]:
            provider = self._calendar_providers[calendar_id]
            async with self._semaphore:
                result = await provider.async_get_day(day)
            if day == today:
                self._record_health(calendar_id, result, now)
            return (calendar_id, day), result

        fetched: dict[tuple[str, date], ScheduleResult] = {}
        failed_today: set[str] = set()
        gathered = await asyncio.gather(
            *(_one(calendar_id, day) for calendar_id, day in pairs),
            return_exceptions=True,
        )
        for (calendar_id, day), item in zip(pairs, gathered, strict=True):
            if isinstance(item, BaseException):
                # The exception can carry a URL in its message, so log only its
                # type (§10). A raised *today* fetch marks the calendar failed so
                # its profiles go unavailable (preserving v1 isolation); a raised
                # tomorrow-preview fetch is just dropped.
                _LOGGER.warning("Calendar fetch failed: %s", type(item).__name__)
                if day == today:
                    failed_today.add(calendar_id)
                    # A provider that crashes every round must still count as
                    # failing, or its repeated-failure repair never appears.
                    self._record_health(
                        calendar_id,
                        ScheduleResult(
                            status="error",
                            events=(),
                            fetched_at=now,
                            error_code=type(item).__name__,
                        ),
                        now,
                    )
                continue
            key, result = item
            fetched[key] = result
        return fetched, failed_today

    def _reconcile_issues(self, data: CoordinatorData) -> None:
        """Create/clear Repairs issues from this round's state (spec §15).

        Source-health issues are keyed per *calendar* so a calendar shared by
        several profiles raises one issue, not one per profile (plan §4.1, F5):

        * invalid source - a calendar whose provider could not be built (missing
          ICS URL or calendar entity);
        * repeated fetch failure - a calendar that failed to fetch at least
          :data:`FETCH_FAILURE_THRESHOLD` times in a row.

        Per-profile issues stay per profile:

        * missing destination - a profile with no destination coordinates;
        * profile without person - a profile not bound to a ``person.*`` entity,
          which cannot match attendee tags in shared calendars (D3).

        The old v1 per-profile ``invalid_source``/``repeated_fetch_failure``
        issue ids are deleted so stale issues do not linger after migration.
        Issues are cleared again the moment the condition no longer holds. The
        placeholders are calendar/profile ids (slugs) and counts only - never a
        calendar or person *name* (review-privacy-security).
        """
        hass = self.hass

        # Clean up the v1 per-profile issue ids now re-keyed per calendar.
        for profile_id in self._known_profile_ids:
            ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_INVALID_SOURCE}_{profile_id}")
            ir.async_delete_issue(
                hass, DOMAIN, f"{ISSUE_REPEATED_FETCH_FAILURE}_{profile_id}"
            )

        # A calendar deleted from the configuration keeps no issue and no health.
        configured = set(self._household.calendars)
        for calendar_id in (self._seen_calendar_ids | set(self._health)) - configured:
            ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_INVALID_SOURCE}_{calendar_id}")
            ir.async_delete_issue(
                hass, DOMAIN, f"{ISSUE_REPEATED_FETCH_FAILURE}_{calendar_id}"
            )
            self._health.pop(calendar_id, None)
        self._seen_calendar_ids = set(configured)

        # Invalid source, per calendar: exactly the unbuildable calendars.
        for calendar_id, reason in self._invalid_calendars.items():
            ir.async_create_issue(
                hass,
                DOMAIN,
                f"{ISSUE_INVALID_SOURCE}_{calendar_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_INVALID_SOURCE,
                translation_placeholders={
                    "calendar": calendar_id,
                    "reason": reason,
                    "linked_profiles": str(
                        self._linked_profile_count.get(calendar_id, 0)
                    ),
                },
            )
        for calendar_id in self._household.calendars:
            if calendar_id in self._invalid_calendars:
                continue
            ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_INVALID_SOURCE}_{calendar_id}")

        # Repeated fetch failures, per calendar.
        for calendar_id in self._household.calendars:
            health = self._health.get(calendar_id)
            failure_id = f"{ISSUE_REPEATED_FETCH_FAILURE}_{calendar_id}"
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
                        "calendar": calendar_id,
                        "failures": str(health.consecutive_failures),
                        "error_code": health.last_error_code or "unknown",
                        "linked_profiles": str(
                            self._linked_profile_count.get(calendar_id, 0)
                        ),
                    },
                )
            else:
                ir.async_delete_issue(hass, DOMAIN, failure_id)

        # Missing destination and missing person, per built profile.
        for runtime in self._runtimes.values():
            config = runtime.config
            missing_dest_id = f"{ISSUE_MISSING_DESTINATION}_{config.id}"
            if _is_missing_destination(config, runtime.destination):
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

            no_person_id = f"{ISSUE_PROFILE_WITHOUT_PERSON}_{config.id}"
            if config.person_entity_id is None:
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    no_person_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_PROFILE_WITHOUT_PERSON,
                    translation_placeholders={"profile": config.id},
                )
            else:
                ir.async_delete_issue(hass, DOMAIN, no_person_id)

    def _persist_round(
        self,
        data: CoordinatorData,
        now: datetime,
        fetched: Mapping[tuple[str, date], ScheduleResult],
    ) -> None:
        """Write the round's schedules, plans and mission state (spec §11.3).

        Persistence is deferred to here (after the revision guard) so a stale
        round never mutates the store. One debounced save flushes all writes.
        Every fetched ``(calendar_id, day)`` is cached unfiltered, once, however
        many profiles link it (plan §2.3); plans and mission state are written
        for every mission of today and, in the evening, of tomorrow.
        """
        for (calendar_id, day), schedule in fetched.items():
            self._store.set_schedule(calendar_id, day, schedule)
        for result in data.values():
            # The mission state is re-read here rather than taken from the
            # round's start: the scheduler may have recorded a notice (or the
            # user confirmed a departure) while the round awaited its providers,
            # and a stale snapshot would wipe that ledger entry and resend.
            # Tomorrow's missions need a state for the ledger and the action
            # nonce of the evening notice (spec §5.5, §11.3).
            for mission in (*result.missions, *result.tomorrow_missions):
                self._persist_mission(
                    mission.plan, self._store.get_mission(mission.plan.mission_id), now
                )
                self._store.set_plan(mission.plan)
            if result.tomorrow_missions:
                self._ensure_digest_state(
                    result.profile.id, result.local_date + timedelta(days=1)
                )
        self._store.async_schedule_save()

    def _ensure_digest_state(self, person_id: str, d: date) -> None:
        """Create the ``person:d:day`` mission behind the day-before digest.

        It holds the digest's ledger and the nonce for its FD_PACKED/FD_OFF
        buttons; an existing one is left alone so its ledger survives rounds.
        """
        mission_id = make_mission_id(person_id, d, DIGEST_SLOT)
        if self._store.get_mission(mission_id) is not None:
            return
        self._store.set_mission(
            MissionState(
                mission_id=mission_id,
                status="scheduled",
                departed_at=None,
                reopened=False,
                notified={},
                first_published_leave=None,
                action_nonce=secrets.token_hex(8),
            )
        )

    async def _process_profile(
        self,
        runtime: _ProfileRuntime,
        today: date,
        now: datetime,
        revision: int,
        fetched: Mapping[tuple[str, date], ScheduleResult],
        failed: set[str],
    ) -> ProfileResult | None:
        """Process one profile in isolation (spec §15: others survive a failure)."""
        try:
            async with runtime.lock:
                return await self._plan_profile(
                    runtime, today, now, revision, fetched, failed
                )
        except Exception:  # noqa: BLE001 - isolate one profile's failure
            # Generic at ERROR; the name-derived profile id only at DEBUG.
            _LOGGER.exception("Failed to update a profile; other profiles continue")
            _LOGGER.debug("Profile update failed for %s", runtime.config.id)
            return None

    async def _plan_profile(
        self,
        runtime: _ProfileRuntime,
        today: date,
        now: datetime,
        revision: int,
        fetched: Mapping[tuple[str, date], ScheduleResult],
        failed: set[str],
    ) -> ProfileResult:
        """Select, pack, plan and persist today's missions from shared fetches.

        The schedule is read from the shared phase-1 fetch map keyed on
        ``(calendar_id, day)`` (plan §4.1), not fetched here, so a calendar
        shared by several profiles is fetched once per round. Health is already
        recorded in phase 1. A main calendar whose fetch *raised* in phase 1
        (in ``failed``) re-raises here so the profile's entities go unavailable,
        matching v1 per-profile isolation (spec §15).

        The schedule is persisted later by ``_persist_round`` (after the
        staleness guard), so the ``previous`` read here is last round's cache for
        cancellation detection (spec §5.2).
        """
        config = runtime.config
        calendar_id = runtime.calendar_id
        if calendar_id is not None and calendar_id in failed:
            # The main calendar's fetch crashed this round: fail this profile so
            # it goes unavailable rather than reading an empty day (spec §5.4).
            raise RuntimeError("main calendar fetch failed")

        previous = (
            self._store.get_schedule(calendar_id, today)
            if calendar_id is not None
            else None
        )
        main_link = config.main_link
        previous_events = None
        if previous is not None:
            previous_events = (
                filter_for_link(
                    previous.events, main_link, self.attendee_aliases(config)
                )
                if main_link is not None
                else previous.events
            )

        day_plan = await self._plan_day(
            runtime, today, now, revision, fetched, previous_events
        )
        active = _active_mission(day_plan.missions, now)
        outcome = RequirementOutcome(
            day_status=day_plan.day.day_status,
            requirement=active.requirement if active is not None else None,
            reason_codes=day_plan.day.reason_codes,
        )

        # Tomorrow's preview in the evening, also on a day without a mission
        # today (e.g. Sunday evening before Monday, spec §5.5, §11.1).
        preview = await self._evening_preview(runtime, today, now, revision, fetched)

        return ProfileResult(
            profile=config,
            outcome=outcome,
            plan=active.plan if active is not None else None,
            packing=day_plan.packing,
            local_date=today,
            schedule=day_plan.main_result,
            calendar_id=calendar_id,
            previous_state=day_plan.main_state,
            tomorrow_plan=preview.plan if preview is not None else None,
            tomorrow_packing=preview.packing if preview is not None else None,
            tomorrow_schedule=preview.schedule if preview is not None else None,
            tomorrow_state=preview.state if preview is not None else None,
            missions=day_plan.missions,
            tomorrow_missions=preview.missions if preview is not None else (),
            failed_extra_links=day_plan.day.failed_links,
            lunch_tomorrow=await self._lunch_tomorrow(config, today, now),
        )

    async def _plan_day(
        self,
        runtime: _ProfileRuntime,
        d: date,
        now: datetime,
        revision: int,
        fetched: Mapping[tuple[str, date], ScheduleResult],
        previous_events: tuple[ScheduleEvent, ...] | None,
    ) -> _DayPlan:
        """Select, plan and annotate every mission of ``d`` for one profile.

        Each link's calendar day is filtered for the profile (pattern and
        attendee filters), the main link keeps first-lesson semantics and every
        extra event becomes its own mission (plan §4.3). Nothing is written to
        the store; the caller persists after its staleness check.
        """
        config = runtime.config
        override = self._store.get_override(config.id, d)
        aliases = self.attendee_aliases(config)
        main_result = self._day_result(runtime.calendar_id, d, now, fetched)

        link_days: dict[str, ScheduleResult] = {}
        for link in config.calendar_links:
            raw = fetched.get((link.calendar_id, d))
            if raw is None:
                if link.role != "main":
                    continue  # unknown for this link: reported as a failed link
                raw = main_result
            link_days[link.link_id] = _filtered(raw, link, aliases)

        # The shared household holiday calendar is not configured in v1, so no
        # automatic "Ledig" day is inferred here; an explicit day-off override
        # still closes the mission (spec §5.4). Wiring a holiday source is a
        # later task.
        main_state = self._store.get_mission(make_mission_id(config.id, d))
        states = {main_state.mission_id: main_state} if main_state is not None else {}
        day = select_missions(
            config,
            d,
            link_days,
            override,
            False,
            previous_events,
            states,
            self._household.locations,
            now,
        )

        planned: list[tuple[str, DeparturePlan, ScheduleEvent | None]] = []
        for mission in day.missions:
            requirement = mission.outcome.requirement
            if requirement is None:
                continue
            plan = await self._plan_mission(
                runtime, requirement, override, now, revision
            )
            planned.append((mission.link_id, plan, mission.event))

        day_events = tuple(
            sorted(
                (e for result in link_days.values() for e in result.events),
                key=lambda e: e.start,
            )
        )
        annotated = annotate_conflicts(
            [plan for _, plan, _ in planned],
            {plan.mission_id: event for _, plan, event in planned if event is not None},
            day_events,
        )
        missions = tuple(
            MissionResult(link_id=link_id, plan=plan, event=event)
            for (link_id, _, event), plan in zip(planned, annotated, strict=True)
        )

        packing = build_packing_list(
            config.id,
            d,
            day_events,
            config.packing_rules,
            override,
            self._store.get_packing_acks(config.id, d),
        )
        return _DayPlan(
            day=day,
            missions=missions,
            packing=packing,
            main_result=main_result,
            main_state=main_state,
        )

    async def _plan_mission(
        self,
        runtime: _ProfileRuntime,
        req: ArrivalRequirement,
        override: DayOverride | None,
        now: datetime,
        revision: int,
    ) -> DeparturePlan:
        """Plan one mission with its own resolved mode, destination and minutes.

        An extra mission whose event is more than :data:`REPLAN_HORIZON` away is
        re-planned at most every :data:`REPLAN_INTERVAL`; in between the stored
        plan is reused so many activities do not multiply journey-planner calls.
        """
        config = runtime.config
        state = self._store.get_mission(req.mission_id)
        if req.slot != MAIN_SLOT and req.event_start - now > REPLAN_HORIZON:
            last = self._last_planned.get(req.mission_id)
            stored = self._store.get_plan(req.mission_id)
            if (
                last is not None
                and now - last < REPLAN_INTERVAL
                and stored is not None
                and stored.config_revision == revision
                and replace(stored.requirement, warnings=()) == req
                # A tap (skip, departed) or a reopen must show at once: reuse
                # only while the stored plan still carries the mission's status.
                and (state is None or _status_unchanged(state, stored))
            ):
                # Conflict flags are recomputed by ``annotate_conflicts`` from
                # today's events; carrying last round's would keep a warning
                # alive after the overlapping event is gone.
                return replace(
                    stored,
                    requirement=replace(stored.requirement, warnings=()),
                    reason_codes=tuple(
                        code
                        for code in stored.reason_codes
                        if code not in CONFLICT_WARNINGS
                    ),
                )

        mode = req.mode or _effective_mode(config, override)
        destination = self._household.locations.get(req.destination_id)
        link = next(
            (lk for lk in config.calendar_links if lk.link_id == req.link_id), None
        )
        plan = await self._plan_for_mode(
            runtime, mode, req, destination, link, state, now, revision
        )
        self._last_planned[req.mission_id] = now
        return replace(plan, config_revision=revision)

    async def _evening_preview(
        self,
        runtime: _ProfileRuntime,
        today: date,
        now: datetime,
        revision: int,
        fetched: Mapping[tuple[str, date], ScheduleResult],
    ) -> _TomorrowPreview | None:
        """Tomorrow's preview from :data:`TOMORROW_PREVIEW_START`, else ``None``.

        A preview failure is logged (profile id at DEBUG only) and never drops
        today's result (spec §15). Tomorrow's day was fetched in phase 1 and is
        read from ``fetched`` rather than re-fetched.
        """
        if now.astimezone(TZ).time() < TOMORROW_PREVIEW_START:
            return None
        try:
            return await self._preview_profile(
                runtime, today + timedelta(days=1), now, revision, fetched
            )
        except Exception:  # noqa: BLE001 - the preview is best effort
            # Profile ids are slugs of display names (often a child's name),
            # so the id is only logged at DEBUG (privacy rule: no person id
            # at INFO+).
            _LOGGER.exception("Failed to compute tomorrow preview for a profile")
            _LOGGER.debug("Tomorrow preview failed for profile %s", runtime.config.id)
            return None

    def _day_result(
        self,
        calendar_id: str | None,
        d: date,
        now: datetime,
        fetched: Mapping[tuple[str, date], ScheduleResult],
    ) -> ScheduleResult:
        """Return a profile's main-calendar day from the shared phase-1 map.

        A CC-only profile (no main link) or a calendar that failed to fetch in
        phase 1 yields an empty result, so planning never blocks on a missing
        entry (spec §15, plan §4.1).
        """
        if calendar_id is None:
            return ScheduleResult(status="empty", events=(), fetched_at=now)
        result = fetched.get((calendar_id, d))
        if result is None:
            return ScheduleResult(status="empty", events=(), fetched_at=now)
        return result

    async def _lunch_tomorrow(
        self, config: ProfileConfig, today: date, now: datetime
    ) -> LunchMenu | None:
        """Tomorrow's menu for a profile with a lunch calendar, from the cache.

        Never raises: a failing lunch source gives an ``error`` menu that the
        digest and sensor simply leave out (plan §7.4).
        """
        entity_id = config.lunch_calendar_entity_id
        if not entity_id:
            return None
        day = today + timedelta(days=1)
        async with self._lunch_lock:
            cached = self._lunch_cache.get((entity_id, day))
            if cached is not None:
                ttl = LUNCH_OK_TTL if cached.status != "error" else LUNCH_ERROR_TTL
                if now - cached.fetched_at < ttl:
                    return cached
            factory = self._factories.lunch or (
                lambda eid: HaCalendarLunchProvider(self.hass, eid)
            )
            try:
                async with self._semaphore:
                    menu = await factory(entity_id).async_get_lunch(day)
            except Exception as err:  # noqa: BLE001 - lunch must never fail a profile
                _LOGGER.warning("Lunch fetch failed: %s", type(err).__name__)
                menu = LunchMenu(
                    local_date=day, status="error", dishes=(), fetched_at=now
                )
            # The provider stamps its own clock; the TTL runs on the round's clock.
            menu = replace(menu, fetched_at=now)
            for key in [k for k in self._lunch_cache if k[1] < today]:
                del self._lunch_cache[key]
            self._lunch_cache[(entity_id, day)] = menu
        return menu

    async def _fetch_calendar_days(
        self, runtime: _ProfileRuntime, d: date, now: datetime
    ) -> dict[tuple[str, date], ScheduleResult]:
        """Fetch one day of every calendar a profile links, under the semaphore.

        Used only by :meth:`async_update_tomorrow_preview`, which runs outside
        the two-phase round and so has no shared fetch map. A calendar whose
        fetch raises is left out, which the day selection reports as a failed
        link. A CC-only profile (no calendar link) has nothing to fetch.
        """
        fetched: dict[tuple[str, date], ScheduleResult] = {}
        for calendar_id in dict.fromkeys(
            link.calendar_id for link in runtime.config.calendar_links
        ):
            provider = self._calendar_providers.get(calendar_id)
            if provider is None:
                continue
            try:
                async with self._semaphore:
                    fetched[(calendar_id, d)] = await provider.async_get_day(d)
            except Exception as err:  # noqa: BLE001 - one calendar must not stop the rest
                _LOGGER.warning("Calendar fetch failed: %s", type(err).__name__)
        return fetched

    def _record_health(
        self, destination_id: str, result: ScheduleResult, now: datetime
    ) -> None:
        """Fold a today-fetch result into the source health counters."""
        health = self._health.setdefault(destination_id, SourceHealth())
        health.record_result(result, now)

    def source_health(self) -> dict[str, SourceHealth]:
        """Expose the per-source health map for diagnostics (T19)."""
        return self._health

    def attendee_aliases(self, config: ProfileConfig) -> frozenset[str]:
        """Return the casefolded aliases that identify ``config``'s person (§4.5).

        Shared-calendar attendee tags (``vem:``/``who:``) name a person
        by display name, HA person friendly name or the person object id. The
        matching set is: the profile display name, and - when the profile is
        bound to a ``person.*`` entity - that entity's ``friendly_name`` and its
        object id (for example ``kid_a`` from ``person.kid_a``). All are
        casefolded. This reads ``hass.states``, so it lives here, not in the
        pure :mod:`calendar_links`; the aliases are person names and are never
        logged (review-privacy-security).
        """
        aliases: set[str] = {config.name.casefold()}
        entity_id = config.person_entity_id
        if entity_id:
            object_id = entity_id.split(".", 1)[-1]
            aliases.add(object_id.casefold())
            state = self.hass.states.get(entity_id)
            if state is not None:
                friendly = state.attributes.get("friendly_name")
                if friendly:
                    aliases.add(str(friendly).casefold())
        return frozenset(aliases)

    @property
    def invalid_sources(self) -> dict[str, str]:
        """Profile ids whose source could not be built (for repairs/diagnostics)."""
        return dict(self._invalid_sources)

    async def _plan_for_mode(
        self,
        runtime: _ProfileRuntime,
        mode: Mode,
        req: ArrivalRequirement,
        destination: Location | None,
        link: CalendarLink | None,
        state: MissionState | None,
        now: datetime,
        revision: int,
    ) -> DeparturePlan:
        """Dispatch to the right planner for ``mode`` (spec §6)."""
        config = runtime.config
        if mode == "public_transport":
            journey = await self._fetch_journey(req, destination, now)
            previous_plan = self._store.get_plan(req.mission_id)
            return plan_transit(
                req, journey, config, state, previous_plan, now, revision
            )

        if mode == "static":
            # Static door-to-door duration; no network (spec §6.4, §11.1).
            duration = self._static_duration(
                config, now, _static_minutes(config, link, destination, req)
            )
            return plan_fixed(req, "static", duration, config, state, now, revision)

        # Car: Waze with the configured reserve-minutes fallback (spec §6.3).
        duration = await self._fetch_car_duration(config, destination, now)
        return plan_fixed(req, "car", duration, config, state, now, revision)

    async def _fetch_journey(
        self, req: ArrivalRequirement, destination: Location | None, now: datetime
    ) -> JourneyResult:
        """Query the SL journey planner for the mission's deadline (spec §6.1)."""
        dest = _coords_of(destination)
        async with self._semaphore:
            return await self._journey.async_plan(
                self._home,
                dest,
                req.arrival_deadline,
                now,
            )

    async def _fetch_car_duration(
        self, config: ProfileConfig, destination: Location | None, now: datetime
    ) -> DurationResult:
        """Get a Waze car duration, falling back to reserve minutes (spec §6.3)."""
        dest = _coords_of(destination)
        async with self._semaphore:
            duration = await self._car.async_get_duration(
                self._home,
                dest,
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

    def _static_duration(
        self, config: ProfileConfig, now: datetime, minutes: int | None
    ) -> DurationResult:
        """Build the fixed static duration for ``minutes`` (spec §6.4)."""
        value = float(minutes) if minutes is not None else None
        return DurationResult(
            minutes=value,
            fetched_at=now,
            source="static",
            quality="scheduled" if value is not None else "unavailable",
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
                    fetched = await self._fetch_calendar_days(runtime, tomorrow, now)
                    preview = await self._preview_profile(
                        runtime, tomorrow, now, revision, fetched
                    )
            except Exception:  # noqa: BLE001 - one preview failing is non-fatal
                _LOGGER.exception("Failed to compute tomorrow preview for a profile")
                _LOGGER.debug(
                    "Tomorrow preview failed for profile %s", runtime.config.id
                )
                continue
            if revision == self._config_revision:
                for (calendar_id, day), schedule in fetched.items():
                    self._store.set_schedule(calendar_id, day, schedule)
            for mission in preview.missions:
                previews[mission.plan.mission_id] = mission.plan
                if revision == self._config_revision:
                    self._store.set_plan(mission.plan)
        return previews

    async def _preview_profile(
        self,
        runtime: _ProfileRuntime,
        d: date,
        now: datetime,
        revision: int,
        fetched: Mapping[tuple[str, date], ScheduleResult],
    ) -> _TomorrowPreview:
        """Build tomorrow's schedule, packing list and preliminary plans.

        Nothing is written to the store here; the caller persists the preview
        after its own staleness check (spec §11.2). ``plan``/``state`` describe
        the main mission's preview (the digest reads ``missions``).
        """
        if runtime.calendar_id is not None and (runtime.calendar_id, d) not in fetched:
            # The main calendar's day could not be fetched: tomorrow is unknown,
            # not empty, so publish no preview (the caller logs and carries on).
            raise RuntimeError("main calendar day not fetched")
        day_plan = await self._plan_day(runtime, d, now, revision, fetched, None)
        main = next(
            (m for m in day_plan.missions if m.requirement.slot == MAIN_SLOT), None
        )
        return _TomorrowPreview(
            schedule=day_plan.main_result,
            packing=day_plan.packing,
            plan=main.plan if main is not None else None,
            state=day_plan.main_state,
            missions=day_plan.missions,
        )

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


def _coords_of(location: Location | None) -> tuple[float, float]:
    """Return a location's coordinates, or (0.0, 0.0) when it is unset."""
    if location is None:
        return (0.0, 0.0)
    return (location.lat, location.lon)


def _is_missing_destination(
    config: ProfileConfig, destination: Location | None
) -> bool:
    """A profile needs destination coordinates for every non-static mode.

    The options flow writes 0.0/0.0 when no location was picked; treat that
    unset origin (or a missing/deleted location) as missing (spec §15: "saknad
    destination"). Static mode uses a fixed duration and does not need
    coordinates.
    """
    if config.default_mode == "static":
        return False
    if destination is None:
        return True
    return destination.lat == 0.0 and destination.lon == 0.0


def _effective_mode(config: ProfileConfig, override: DayOverride | None) -> Mode:
    """Return the mode for a day: a day override's mode wins (spec §4.3)."""
    if override is not None and override.mode is not None:
        return override.mode
    return config.default_mode


def _filtered(
    raw: ScheduleResult, link: CalendarLink, aliases: frozenset[str]
) -> ScheduleResult:
    """``raw`` narrowed to what ``link`` applies to this profile (plan §4.1).

    An ``ok`` day that the filters empty becomes ``empty`` so it reads as
    "nothing registered", never as a fetch error.
    """
    events = filter_for_link(raw.events, link, aliases)
    status = "empty" if raw.status == "ok" and not events else raw.status
    return replace(raw, status=status, events=events)


def _status_unchanged(state: MissionState, stored: DeparturePlan) -> bool:
    """True when no user action has changed the mission since ``stored``."""
    return (
        state.status == stored.status
        and state.status not in ("departed", "skipped")
        and not state.reopened
    )


def _active_mission(
    missions: tuple[MissionResult, ...], now: datetime
) -> MissionResult | None:
    """The mission the profile's sensors describe now.

    The earliest mission that is neither departed nor skipped and whose event is
    not past the mission timeout. When every mission is done, the main mission
    (else the first) stays published, as the single-mission behaviour always did.
    """
    live = [
        m
        for m in missions
        if m.plan.status not in ("departed", "skipped")
        and now < m.requirement.event_start + timedelta(minutes=MISSION_TIMEOUT_MINUTES)
    ]
    if live:
        return min(live, key=lambda m: m.requirement.event_start)
    for mission in missions:
        if mission.requirement.slot == MAIN_SLOT:
            return mission
    return missions[0] if missions else None


def _static_minutes(
    config: ProfileConfig,
    link: CalendarLink | None,
    destination: Location | None,
    req: ArrivalRequirement,
) -> int | None:
    """Static minutes: link default (only at the link's own destination) >
    location > profile (plan §4.7)."""
    link_destination = (
        (link.location_id or config.default_location_id) if link is not None else None
    )
    if (
        link is not None
        and link.static_minutes is not None
        and req.destination_id == link_destination
    ):
        return link.static_minutes
    if destination is not None and destination.static_minutes is not None:
        return destination.static_minutes
    return config.static_minutes


def _mission_id_for(person_id: str, d: date) -> str:
    """Local import-free mission id (keeps this module's call sites explicit)."""
    from .timeutil import make_mission_id

    return make_mission_id(person_id, d)


__all__ = [
    "CoordinatorData",
    "FamilyDeparturesCoordinator",
    "ProfileResult",
    "ProviderFactories",
    "SourceHealth",
]
