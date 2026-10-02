"""Waze car travel-time provider (spec §6.3).

Wraps Home Assistant's ``waze_travel_time.get_travel_times`` action to produce
the integration's :class:`..models.DurationResult` for the ``car`` mode. The
action returns a list of candidate routes with a ``duration`` in minutes; this
provider picks the shortest valid route and keeps its name for ``route_name``
(``route_summary`` on the plan).

Rules honoured here (spec §6.3, review-journey-planning):

* Call via ``hass.services.async_call(..., blocking=True, return_response=True)``
  with ``region: "eu"`` and ``realtime`` (true in the morning window). The
  evening plan may pass ``realtime=False`` with a ``time_delta`` for a
  statistical estimate; that value is marked ``quality="estimated"`` because it
  is preliminary, never a live forecast.
* A missing response, an error, an empty route list or a non-numeric duration is
  reported as ``minutes=None`` -- never silently treated as zero travel time.
  Deriving a safe departure from a missing duration is the planner's job
  (``needs_configuration`` / fallback), not this provider's.
* Results are cached per ``(origin, destination, realtime, time_delta)`` for 10
  minutes so a route shared by several profiles is only fetched once per update
  round, and concurrent calls for the same key are de-duplicated (single
  flight). Parking and walking are added by the planner, never here.

This module imports ``homeassistant`` (it calls a HA service) but never reads
the wall clock on its own: the caller passes ``now`` so cache freshness stays
testable with controlled time.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from numbers import Real
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..models import DurationResult, Quality

_LOGGER = logging.getLogger(__name__)

WAZE_DOMAIN = "waze_travel_time"
SERVICE_GET_TRAVEL_TIMES = "get_travel_times"

# Default region for Stockholm routes (spec §6.3).
DEFAULT_REGION = "eu"

# How long a fetched duration stays fresh and may be reused (spec §6.3: a shared
# route is called once per update round; the review skill fixes the TTL at
# 10 min).
CACHE_TTL = timedelta(minutes=10)

# Internal cache key: origin, destination, realtime flag and optional time_delta
# in whole seconds (``None`` when not supplied).
_CacheKey = tuple[tuple[float, float], tuple[float, float], bool, int | None]


class _CacheEntry:
    """A cached duration plus the time it was fetched."""

    __slots__ = ("fetched_at", "result")

    def __init__(self, result: DurationResult, fetched_at: datetime) -> None:
        self.result = result
        self.fetched_at = fetched_at


def _coerce_minutes(value: Any) -> float | None:
    """Return ``value`` as a float of minutes, or ``None`` when not numeric.

    A string, ``None``, a boolean or any non-real value is rejected (spec §6.3:
    never interpret a missing or non-numeric duration as zero). Negative values
    are also rejected as implausible.
    """
    # ``bool`` is a subclass of ``int``; a boolean duration is never valid.
    if isinstance(value, bool):
        return None
    if not isinstance(value, Real):
        return None
    minutes = float(value)
    if minutes < 0:
        return None
    return minutes


def _extract_routes(response: Any) -> list[dict[str, Any]]:
    """Pull the list of route dicts out of a ``get_travel_times`` response.

    The action returns ``{"routes": [...]}``; be defensive about an entity- or
    target-keyed wrapper too. Anything that is not a list of dicts yields an
    empty list, which the caller treats as "no route" (``minutes=None``).
    """
    if not isinstance(response, dict):
        return []
    routes = response.get("routes")
    if isinstance(routes, list):
        return [route for route in routes if isinstance(route, dict)]
    # Fallback: a single wrapper value that itself carries ``routes``.
    for value in response.values():
        if isinstance(value, dict) and isinstance(value.get("routes"), list):
            return [route for route in value["routes"] if isinstance(route, dict)]
    return []


def _shortest_route(routes: list[dict[str, Any]]) -> tuple[float, str | None] | None:
    """Return ``(minutes, name)`` for the shortest valid route, or ``None``.

    Routes with a non-numeric duration are skipped rather than failing the whole
    call. ``None`` means no route had a usable duration.
    """
    best: tuple[float, str | None] | None = None
    for route in routes:
        minutes = _coerce_minutes(route.get("duration"))
        if minutes is None:
            continue
        name = route.get("name")
        route_name = name if isinstance(name, str) and name else None
        if best is None or minutes < best[0]:
            best = (minutes, route_name)
    return best


class WazeCarProvider:
    """Door-to-door car durations via ``waze_travel_time.get_travel_times``.

    Implements the :class:`..providers.base.CarProvider` protocol. One instance
    is shared across profiles so the per-route cache and single-flight locking
    deduplicate calls for a route used by more than one person.
    """

    def __init__(self, hass: HomeAssistant, *, region: str = DEFAULT_REGION) -> None:
        self._hass = hass
        self._region = region
        self._cache: dict[_CacheKey, _CacheEntry] = {}
        # One lock per cache key so concurrent requests for the same route wait
        # for the first fetch instead of all hitting the service.
        self._locks: dict[_CacheKey, asyncio.Lock] = {}

    async def async_get_duration(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        realtime: bool,
        time_delta: timedelta | None,
        *,
        now: datetime | None = None,
    ) -> DurationResult:
        """Return the car travel duration for ``origin`` -> ``destination``.

        ``realtime`` selects the morning live mode (``True``) or the preliminary
        evening estimate (``False``), and ``time_delta`` offsets the evening
        query to the next departure time. ``now`` controls cache freshness and
        the result timestamp; it defaults to the current UTC time.
        """
        moment = now or datetime.now(UTC)
        key = self._cache_key(origin, destination, realtime, time_delta)

        cached = self._fresh_cached(key, moment)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Another waiter may have populated the cache while we waited.
            cached = self._fresh_cached(key, moment)
            if cached is not None:
                return cached
            result = await self._fetch(
                origin, destination, realtime, time_delta, moment
            )
            self._cache[key] = _CacheEntry(result, moment)
            return result

    def _cache_key(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        realtime: bool,
        time_delta: timedelta | None,
    ) -> _CacheKey:
        delta = int(time_delta.total_seconds()) if time_delta is not None else None
        return (origin, destination, realtime, delta)

    def _fresh_cached(self, key: _CacheKey, now: datetime) -> DurationResult | None:
        """Return a cached result still within the TTL, retimestamped to ``now``."""
        entry = self._cache.get(key)
        if entry is None or now - entry.fetched_at >= CACHE_TTL:
            return None
        # Keep the cached minutes/source/quality/route but surface the current
        # time so callers see when the value was served.
        prior = entry.result
        return DurationResult(
            minutes=prior.minutes,
            fetched_at=now,
            source=prior.source,
            quality=prior.quality,
            route_name=prior.route_name,
        )

    async def _fetch(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        realtime: bool,
        time_delta: timedelta | None,
        now: datetime,
    ) -> DurationResult:
        """Call the Waze action once and map the result.

        An error, empty route list or non-numeric duration yields
        ``minutes=None`` (spec §6.3). A live realtime answer is
        ``quality="realtime"``; the preliminary evening estimate is
        ``quality="estimated"``.
        """
        quality: Quality = "realtime" if realtime else "estimated"
        data: dict[str, Any] = {
            "origin": f"{origin[0]},{origin[1]}",
            "destination": f"{destination[0]},{destination[1]}",
            "region": self._region,
            "realtime": realtime,
        }
        if time_delta is not None:
            data["time_delta"] = int(time_delta.total_seconds())

        try:
            response = await self._hass.services.async_call(
                WAZE_DOMAIN,
                SERVICE_GET_TRAVEL_TIMES,
                data,
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            # ServiceNotFound (integration not configured) and any raised service
            # error map to an unavailable duration, never zero.
            _LOGGER.warning(
                "waze_travel_time.get_travel_times failed (%s)", type(err).__name__
            )
            return DurationResult(
                minutes=None,
                fetched_at=now,
                source="waze",
                quality="unavailable",
            )

        best = _shortest_route(_extract_routes(response))
        if best is None:
            _LOGGER.warning("waze_travel_time returned no usable route")
            return DurationResult(
                minutes=None,
                fetched_at=now,
                source="waze",
                quality="unavailable",
            )

        minutes, route_name = best
        return DurationResult(
            minutes=minutes,
            fetched_at=now,
            source="waze",
            quality=quality,
            route_name=route_name,
        )
