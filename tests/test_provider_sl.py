"""Tests for the SL Journey Planner v2 adapter (spec §6.1, §11.2).

Mapping is tested against the sanitised T00 fixtures in ``tests/fixtures/sl``.
The request builder is locked by a contract test. Fetching is driven through a
small fake aiohttp session so timeouts, 429 and HTTP errors are exercised
without the network. A single opt-in live smoke test runs only when
``FD_LIVE_SL=1`` is set.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from custom_components.family_departures.providers.sl_journey import (
    ERROR_CONNECTION,
    ERROR_HTTP,
    ERROR_RATE_LIMITED,
    ERROR_RESPONSE,
    ERROR_TIMEOUT,
    SlJourneyProvider,
    build_trip_params,
    map_journeys,
)
from custom_components.family_departures.timeutil import TZ

from sl_coords import DESTINATION, ORIGIN

FIXTURES = Path(__file__).parent / "fixtures" / "sl"

# A deadline in winter (CET, +01:00) and one in summer (CEST, +02:00).
DEADLINE_WINTER = datetime(2026, 11, 2, 7, 20, tzinfo=UTC)  # 08:20 local
DEADLINE_SUMMER = datetime(2026, 7, 1, 6, 20, tzinfo=UTC)  # 08:20 local


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Request builder (contract test – locks parameter names and formatting)
# --------------------------------------------------------------------------


def test_build_trip_params_arrival_contract() -> None:
    params = build_trip_params(ORIGIN, DESTINATION, DEADLINE_WINTER)

    # Arrival-time search is selected explicitly.
    assert params["itd_trip_date_time_dep_arr"] == "arr"
    # Coordinates: longitude first, five decimals, WGS84 suffix.
    assert params["type_origin"] == "coord"
    assert params["name_origin"] == "18.06490:59.33258:WGS84[dd.ddddd]"
    assert params["type_destination"] == "coord"
    assert params["name_destination"] == "18.04960:59.34300:WGS84[dd.ddddd]"
    # At most three alternatives, Swedish texts.
    assert params["calc_number_of_trips"] == "3"
    assert params["language"] == "sv"


def test_build_trip_params_local_time_conversion_winter() -> None:
    """A UTC deadline becomes local Europe/Stockholm date/time (winter +01:00)."""
    params = build_trip_params(ORIGIN, DESTINATION, DEADLINE_WINTER)
    assert params["itd_date"] == "20261102"
    assert params["itd_time"] == "0820"


def test_build_trip_params_local_time_conversion_summer() -> None:
    """The same wall-clock in summer uses +02:00 for the UTC->local shift."""
    params = build_trip_params(ORIGIN, DESTINATION, DEADLINE_SUMMER)
    assert params["itd_date"] == "20260701"
    assert params["itd_time"] == "0820"
    # Sanity: the local wall-clock really is 08:20.
    assert DEADLINE_SUMMER.astimezone(TZ).strftime("%H%M") == "0820"


# --------------------------------------------------------------------------
# Mapping (against T00 fixtures)
# --------------------------------------------------------------------------


def test_map_normal_journeys() -> None:
    journeys, has_realtime = map_journeys(_load("arrival_normal.json"))

    assert len(journeys) == 3
    assert has_realtime is False  # estimated == planned throughout.

    first = journeys[0]
    kinds = [leg.kind for leg in first.legs]
    assert kinds == ["walk", "walk", "transit", "walk", "walk"]

    transit = next(leg for leg in first.legs if leg.kind == "transit")
    assert transit.line == "17"
    assert transit.direction == "Vällingby"
    assert transit.from_stop == "9025001000002051"  # namespaced stop id
    assert transit.to_stop == "9025001000001131"
    assert transit.platform == "1"
    assert transit.trip_ref == "tfs:02017: :H:y01"
    assert transit.cancelled is False
    assert transit.planned_departure == datetime(2026, 10, 5, 6, 7, 54, tzinfo=UTC)
    assert transit.planned_arrival == datetime(2026, 10, 5, 6, 12, 12, tzinfo=UTC)


def test_map_realtime_sets_has_realtime() -> None:
    """A MONITORED leg whose estimate differs from planned flips has_realtime."""
    journeys, has_realtime = map_journeys(_load("realtime_monitored.json"))

    assert journeys
    assert has_realtime is True

    transit = next(leg for leg in journeys[0].legs if leg.kind == "transit")
    assert transit.estimated_departure is not None
    assert transit.planned_departure is not None
    assert transit.estimated_departure > transit.planned_departure


def test_map_cancelled_leg() -> None:
    journeys, _ = map_journeys(_load("cancelled_deviation.json"))

    transit = next(leg for leg in journeys[0].legs if leg.kind == "transit")
    assert transit.cancelled is True


def test_journey_id_is_stable_for_same_trip() -> None:
    """The journey id derives from the first transit trip ref + departure."""
    journeys, _ = map_journeys(_load("arrival_normal.json"))
    first_again, _ = map_journeys(_load("arrival_normal.json"))
    assert journeys[0].journey_id == first_again[0].journey_id
    assert journeys[0].journey_id.startswith("tfs:02017: :H:y01@")


def test_error_body_maps_to_empty() -> None:
    """An error-shaped body (no ``journeys`` key) maps to no journeys."""
    journeys, has_realtime = map_journeys(_load("error_invalid_date.json"))
    assert journeys == ()
    assert has_realtime is False


# --------------------------------------------------------------------------
# Fake aiohttp session for provider tests
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        status: int = 200,
        payload: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def json(self, content_type: str | None = None) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse | Exception]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        timeout: object = None,
    ) -> _FakeResponse:
        self.calls.append((url, dict(params or {})))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _provider(session: _FakeSession) -> SlJourneyProvider:
    return SlJourneyProvider(session)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# async_plan
# --------------------------------------------------------------------------


async def test_async_plan_ok() -> None:
    session = _FakeSession([_FakeResponse(200, _load("arrival_normal.json"))])
    provider = _provider(session)

    result = await provider.async_plan(ORIGIN, DESTINATION, DEADLINE_WINTER, None)

    assert result.status == "ok"
    assert len(result.journeys) == 3
    assert result.has_realtime is False
    assert result.error_code is None
    # The request carried the locked arrival parameters.
    _, params = session.calls[0]
    assert params["itd_trip_date_time_dep_arr"] == "arr"
    assert params["itd_date"] == "20261102"


async def test_async_plan_realtime_flag() -> None:
    session = _FakeSession([_FakeResponse(200, _load("realtime_monitored.json"))])
    provider = _provider(session)

    result = await provider.async_plan(ORIGIN, DESTINATION, DEADLINE_WINTER, None)

    assert result.status == "ok"
    assert result.has_realtime is True


async def test_async_plan_error_response_body() -> None:
    session = _FakeSession([_FakeResponse(200, _load("error_invalid_date.json"))])
    provider = _provider(session)

    result = await provider.async_plan(ORIGIN, DESTINATION, DEADLINE_WINTER, None)

    assert result.status == "error"
    assert result.error_code == ERROR_RESPONSE
    assert result.journeys == ()


async def test_async_plan_timeout() -> None:
    session = _FakeSession([TimeoutError()])
    provider = _provider(session)

    result = await provider.async_plan(ORIGIN, DESTINATION, DEADLINE_WINTER, None)

    assert result.status == "error"
    assert result.error_code == ERROR_TIMEOUT
    assert result.journeys == ()


async def test_async_plan_rate_limited() -> None:
    session = _FakeSession([_FakeResponse(429, None, {"Retry-After": "30"})])
    provider = _provider(session)

    result = await provider.async_plan(ORIGIN, DESTINATION, DEADLINE_WINTER, None)

    assert result.status == "error"
    assert result.error_code == ERROR_RATE_LIMITED


async def test_async_plan_server_error() -> None:
    session = _FakeSession([_FakeResponse(503, None)])
    provider = _provider(session)

    result = await provider.async_plan(ORIGIN, DESTINATION, DEADLINE_WINTER, None)

    assert result.status == "error"
    assert result.error_code == ERROR_HTTP


async def test_async_plan_connection_error() -> None:
    session = _FakeSession([aiohttp.ClientError()])
    provider = _provider(session)

    result = await provider.async_plan(ORIGIN, DESTINATION, DEADLINE_WINTER, None)

    assert result.status == "error"
    assert result.error_code == ERROR_CONNECTION


async def test_async_plan_filters_by_earliest_departure() -> None:
    """Candidates boarding before ``earliest_departure`` are dropped."""
    session = _FakeSession([_FakeResponse(200, _load("arrival_normal.json"))])
    provider = _provider(session)

    # All fixture journeys depart around 06:02-06:08 UTC; a later floor drops them.
    earliest = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
    result = await provider.async_plan(ORIGIN, DESTINATION, DEADLINE_WINTER, earliest)

    assert result.status == "empty"
    assert result.journeys == ()


# --------------------------------------------------------------------------
# Opt-in live smoke test
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("FD_LIVE_SL") != "1",
    reason="live SL smoke test only runs with FD_LIVE_SL=1",
)
async def test_live_sl_smoke() -> None:  # pragma: no cover - network dependent
    """Hit the real endpoint for tomorrow 08:20 and expect a usable response."""
    from datetime import time, timedelta

    from custom_components.family_departures.timeutil import combine_local

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).astimezone(TZ).date()
    deadline = combine_local(tomorrow, time(8, 20))
    async with aiohttp.ClientSession() as session:
        provider = SlJourneyProvider(session)
        result = await provider.async_plan(ORIGIN, DESTINATION, deadline, None)
    assert result.status in ("ok", "empty")
