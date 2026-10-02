"""Tests for the Waze car travel-time provider (spec §6.3).

These exercise the provider against a mocked ``waze_travel_time.get_travel_times``
service so the response handling (shortest route, empty list, non-numeric
duration, ServiceNotFound), the 10-minute cache and single-flight dedup are
covered without a real Waze integration or network.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.family_departures.providers.waze import (
    SERVICE_GET_TRAVEL_TIMES,
    WAZE_DOMAIN,
    WazeCarProvider,
)
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)

ORIGIN = (59.3, 18.0)
DEST = (59.4, 18.1)
NOW = datetime(2026, 10, 20, 5, 30, tzinfo=UTC)


class _ServiceRecorder:
    """Records calls to the fake get_travel_times service."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []


def _register(hass: HomeAssistant, response: Any) -> _ServiceRecorder:
    recorder = _ServiceRecorder()

    async def _handle(call: ServiceCall) -> ServiceResponse:
        recorder.calls.append(dict(call.data))
        return response

    hass.services.async_register(
        WAZE_DOMAIN,
        SERVICE_GET_TRAVEL_TIMES,
        _handle,
        supports_response=SupportsResponse.ONLY,
    )
    return recorder


async def test_normal_shortest_route(hass: HomeAssistant) -> None:
    """The shortest valid route is picked and its name kept."""
    _register(
        hass,
        {
            "routes": [
                {"duration": 25.0, "name": "Via E4"},
                {"duration": 19.5, "name": "Via Essingeleden"},
            ]
        },
    )
    provider = WazeCarProvider(hass)
    result = await provider.async_get_duration(ORIGIN, DEST, True, None, now=NOW)

    assert result.minutes == 19.5
    assert result.route_name == "Via Essingeleden"
    assert result.source == "waze"
    assert result.quality == "realtime"
    assert result.fetched_at == NOW


async def test_empty_route_list_is_none(hass: HomeAssistant) -> None:
    """An empty route list is never treated as zero travel time."""
    _register(hass, {"routes": []})
    provider = WazeCarProvider(hass)
    result = await provider.async_get_duration(ORIGIN, DEST, True, None, now=NOW)

    assert result.minutes is None
    assert result.source == "waze"
    assert result.quality == "unavailable"


async def test_non_numeric_duration_is_none(hass: HomeAssistant) -> None:
    """A string duration is rejected rather than coerced to zero."""
    _register(hass, {"routes": [{"duration": "20 min", "name": "Via E4"}]})
    provider = WazeCarProvider(hass)
    result = await provider.async_get_duration(ORIGIN, DEST, True, None, now=NOW)

    assert result.minutes is None
    assert result.quality == "unavailable"


async def test_mixed_routes_skip_bad_duration(hass: HomeAssistant) -> None:
    """A bad duration is skipped; a valid shorter route still wins."""
    _register(
        hass,
        {
            "routes": [
                {"duration": None, "name": "Broken"},
                {"duration": 30, "name": "Long"},
                {"duration": "x", "name": "Also broken"},
            ]
        },
    )
    provider = WazeCarProvider(hass)
    result = await provider.async_get_duration(ORIGIN, DEST, True, None, now=NOW)

    assert result.minutes == 30.0
    assert result.route_name == "Long"


async def test_service_not_found_is_none(hass: HomeAssistant) -> None:
    """A missing waze integration (ServiceNotFound) yields minutes=None."""
    provider = WazeCarProvider(hass)
    result = await provider.async_get_duration(ORIGIN, DEST, True, None, now=NOW)

    assert result.minutes is None
    assert result.source == "waze"
    assert result.quality == "unavailable"


async def test_cache_hit_within_ttl(hass: HomeAssistant) -> None:
    """A second call within 10 minutes reuses the cached value (one service call)."""
    recorder = _register(hass, {"routes": [{"duration": 21.0, "name": "Via E4"}]})
    provider = WazeCarProvider(hass)

    first = await provider.async_get_duration(ORIGIN, DEST, True, None, now=NOW)
    later = NOW + timedelta(minutes=9)
    second = await provider.async_get_duration(ORIGIN, DEST, True, None, now=later)

    assert len(recorder.calls) == 1
    assert first.minutes == second.minutes == 21.0
    # The cached value is retimestamped to the serving moment.
    assert second.fetched_at == later


async def test_cache_expires_after_ttl(hass: HomeAssistant) -> None:
    """After the TTL the service is called again."""
    recorder = _register(hass, {"routes": [{"duration": 21.0, "name": "Via E4"}]})
    provider = WazeCarProvider(hass)

    await provider.async_get_duration(ORIGIN, DEST, True, None, now=NOW)
    later = NOW + timedelta(minutes=10)
    await provider.async_get_duration(ORIGIN, DEST, True, None, now=later)

    assert len(recorder.calls) == 2


async def test_single_call_for_shared_route(hass: HomeAssistant) -> None:
    """Two concurrent profiles sharing a route trigger a single service call."""
    recorder = _register(hass, {"routes": [{"duration": 18.0, "name": "Via E4"}]})
    provider = WazeCarProvider(hass)

    results = await asyncio.gather(
        provider.async_get_duration(ORIGIN, DEST, True, None, now=NOW),
        provider.async_get_duration(ORIGIN, DEST, True, None, now=NOW),
    )

    assert len(recorder.calls) == 1
    assert {r.minutes for r in results} == {18.0}


async def test_realtime_false_is_preliminary_estimate(hass: HomeAssistant) -> None:
    """The evening query (realtime=False + time_delta) is marked estimated."""
    recorder = _register(hass, {"routes": [{"duration": 22.0, "name": "Via E4"}]})
    provider = WazeCarProvider(hass)

    result = await provider.async_get_duration(
        ORIGIN, DEST, False, timedelta(hours=14), now=NOW
    )

    assert result.quality == "estimated"
    assert result.source == "waze"
    assert result.minutes == 22.0
    call = recorder.calls[0]
    assert call["realtime"] is False
    assert call["region"] == "eu"
    assert call["time_delta"] == int(timedelta(hours=14).total_seconds())


async def test_realtime_and_evening_cached_separately(hass: HomeAssistant) -> None:
    """Realtime and evening queries use distinct cache keys."""
    recorder = _register(hass, {"routes": [{"duration": 20.0, "name": "Via E4"}]})
    provider = WazeCarProvider(hass)

    await provider.async_get_duration(ORIGIN, DEST, True, None, now=NOW)
    await provider.async_get_duration(ORIGIN, DEST, False, timedelta(hours=14), now=NOW)

    assert len(recorder.calls) == 2
