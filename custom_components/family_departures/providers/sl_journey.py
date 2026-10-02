"""SL Journey Planner v2 adapter (spec §6.1, §11.2).

This adapter turns the arrival-time search of Trafiklab's **SL Journey Planner
v2** into the integration's ``JourneyResult``/``Journey``/``Leg`` contract. The
request contract (parameter names, date/time formats, time-zone handling and the
``<lon>:<lat>:WGS84[dd.ddddd]`` coordinate order) was verified and locked in
``docs/decisions/sl-realtime.md`` (task T00); a contract test freezes it.

Design (per the T00 decision **A**):

* No separate realtime adapter and no §6.2 trip matching. Realtime arrives as
  per-leg ``estimated`` times on the *same* arrival search, so this module only
  maps what the response carries. The §6.2 cancellation / transfer rules that
  are source-independent live in :mod:`..planner` (``plan_transit``), not here.
* The module is HA-free: it takes an injected :class:`aiohttp.ClientSession`.
  It is *not* pure (it does I/O), but it never reads the wall clock and takes no
  ``now`` parameter; the ``deadline`` it is given is already aware UTC.

Robustness (spec §11.2): 10 s timeout, ``429`` with ``Retry-After`` surfaced as
an error (the coordinator applies backoff), and any timeout / connection / HTTP
error or error-shaped body (``journeys`` absent) becomes ``status="error"`` with
no journeys. Missing realtime is reported as ``has_realtime=False`` and never
invented.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import aiohttp

from ..models import Journey, JourneyResult, Leg
from ..timeutil import TZ

_LOGGER = logging.getLogger(__name__)

# Base URL for the SL Journey Planner v2 trip search (docs/decisions/sl-realtime.md).
BASE_URL = "https://journeyplanner.integration.sl.se/v2"
TRIPS_PATH = "/trips"

# Request limits (spec §11.2).
REQUEST_TIMEOUT_SECONDS = 10.0

# At most three alternatives per search (spec §6.1).
MAX_TRIPS = 3

# ``transportation.product.class`` values 99/100 are footpaths; everything else
# is a transit mode (docs/decisions/sl-realtime.md).
_WALK_PRODUCT_CLASSES: frozenset[int] = frozenset((99, 100))

# EFA cancellation tokens as they appear in ``realtimeStatus``. ``TRIP_CANCELLED``
# is provisional (synthetic fixture) and must be re-verified against a real
# disruption; the extra tokens are defensive aliases seen in EFA deployments.
_CANCELLED_STATUS_TOKENS: frozenset[str] = frozenset(
    ("TRIP_CANCELLED", "CANCELLED", "TRIP_DELETED")
)

# Error codes surfaced on ``JourneyResult`` so the coordinator can distinguish
# failure modes without exposing provider internals.
ERROR_TIMEOUT = "timeout"
ERROR_HTTP = "http_error"
ERROR_CONNECTION = "connection_error"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_RESPONSE = "error_response"
ERROR_DECODE = "decode_error"


def _format_coord(lat: float, lon: float) -> str:
    """Format a WGS84 coordinate as JP v2 expects: longitude first.

    The documented form is ``<lon>:<lat>:WGS84[dd.ddddd]`` (longitude before
    latitude). Five decimals match the capture in the decision doc.
    """
    return f"{lon:.5f}:{lat:.5f}:WGS84[dd.ddddd]"


def build_trip_params(
    origin: tuple[float, float],
    destination: tuple[float, float],
    deadline: datetime,
    *,
    number_of_trips: int = MAX_TRIPS,
) -> dict[str, str]:
    """Build the query parameters for an arrival-time trip search.

    ``deadline`` is an aware UTC datetime; ``itd_date``/``itd_time`` are the
    deadline converted to local ``Europe/Stockholm`` wall-clock, because the API
    interprets the request time in local time (docs/decisions/sl-realtime.md).
    This mapping is what the T09 contract test locks.
    """
    local_deadline = deadline.astimezone(TZ)
    return {
        "type_origin": "coord",
        "name_origin": _format_coord(origin[0], origin[1]),
        "type_destination": "coord",
        "name_destination": _format_coord(destination[0], destination[1]),
        "calc_number_of_trips": str(max(1, min(number_of_trips, MAX_TRIPS))),
        "itd_date": local_deadline.strftime("%Y%m%d"),
        "itd_time": local_deadline.strftime("%H%M"),
        "itd_trip_date_time_dep_arr": "arr",
        "language": "sv",
    }


def _parse_utc(value: Any) -> datetime | None:
    """Parse an ISO 8601 ``...Z`` timestamp into aware UTC, or ``None``.

    Response times are UTC with a trailing ``Z`` (docs/decisions/sl-realtime.md).
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_cancelled(leg: dict[str, Any]) -> bool:
    """Return whether a leg carries a cancellation token in ``realtimeStatus``."""
    statuses = leg.get("realtimeStatus")
    if not isinstance(statuses, list):
        return False
    return any(
        isinstance(status, str) and status.upper() in _CANCELLED_STATUS_TOKENS
        for status in statuses
    )


def _leg_has_realtime(leg: dict[str, Any]) -> bool:
    """Whether a leg is actually realtime-controlled with an estimate.

    True only when the API flags the leg as realtime-controlled *and* an
    estimated time differs from the planned time; a planned==estimated echo is
    not treated as live data (spec §6.2: missing realtime is unknown, not zero).
    """
    if not leg.get("isRealtimeControlled"):
        return False
    origin = leg.get("origin") or {}
    destination = leg.get("destination") or {}
    for node, planned_key, est_key in (
        (origin, "departureTimePlanned", "departureTimeEstimated"),
        (destination, "arrivalTimePlanned", "arrivalTimeEstimated"),
    ):
        planned = _parse_utc(node.get(planned_key))
        estimated = _parse_utc(node.get(est_key))
        if planned is not None and estimated is not None and planned != estimated:
            return True
    return False


def _transit_name(transportation: dict[str, Any]) -> str | None:
    """Short line label, preferring the disassembled name (e.g. ``17``)."""
    for key in ("disassembledName", "number", "name"):
        value = transportation.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _map_leg(leg: dict[str, Any]) -> Leg:
    """Map one JP v2 leg to a :class:`Leg` with namespaced stop IDs."""
    origin = leg.get("origin") or {}
    destination = leg.get("destination") or {}
    transportation = leg.get("transportation") or {}
    product = transportation.get("product") or {}
    product_class = product.get("class")
    is_walk = not transportation or (
        isinstance(product_class, int) and product_class in _WALK_PRODUCT_CLASSES
    )

    if is_walk:
        return Leg(
            kind="walk",
            from_stop=origin.get("id"),
            to_stop=destination.get("id"),
            planned_departure=_parse_utc(origin.get("departureTimePlanned")),
            estimated_departure=_parse_utc(origin.get("departureTimeEstimated")),
            planned_arrival=_parse_utc(destination.get("arrivalTimePlanned")),
            estimated_arrival=_parse_utc(destination.get("arrivalTimeEstimated")),
        )

    direction_node = transportation.get("destination") or {}
    direction = direction_node.get("name") if isinstance(direction_node, dict) else None
    origin_props = origin.get("properties") or {}
    properties = leg.get("properties") or {}
    trip_ref = transportation.get("id") or properties.get("tripId")

    return Leg(
        kind="transit",
        line=_transit_name(transportation),
        direction=direction,
        from_stop=origin.get("id"),
        to_stop=destination.get("id"),
        platform=origin_props.get("platformName") or origin_props.get("platform"),
        planned_departure=_parse_utc(origin.get("departureTimePlanned")),
        estimated_departure=_parse_utc(origin.get("departureTimeEstimated")),
        planned_arrival=_parse_utc(destination.get("arrivalTimePlanned")),
        estimated_arrival=_parse_utc(destination.get("arrivalTimeEstimated")),
        cancelled=_is_cancelled(leg),
        trip_ref=trip_ref if isinstance(trip_ref, str) else None,
    )


def _journey_id(index: int, legs: tuple[Leg, ...]) -> str:
    """Stable id for a journey, used by the planner for stickiness (§6.2).

    Prefer the first transit leg's trip ref and planned departure so the same
    vehicle keeps the same id across re-queries; fall back to the ordinal.
    """
    for leg in legs:
        if leg.kind == "transit" and leg.trip_ref:
            stamp = leg.planned_departure.isoformat() if leg.planned_departure else "?"
            return f"{leg.trip_ref}@{stamp}"
    return f"journey-{index}"


def map_journeys(payload: dict[str, Any]) -> tuple[tuple[Journey, ...], bool]:
    """Map a decoded JP v2 payload to journeys plus a ``has_realtime`` flag.

    ``has_realtime`` is true when *any* mapped leg is realtime-controlled with an
    estimate (spec §6.1: the flag reflects actual estimated fields, not merely
    that a response came back).
    """
    raw_journeys = payload.get("journeys")
    if not isinstance(raw_journeys, list):
        return (), False

    journeys: list[Journey] = []
    has_realtime = False
    for index, raw in enumerate(raw_journeys[:MAX_TRIPS]):
        if not isinstance(raw, dict):
            continue
        raw_legs = raw.get("legs")
        if not isinstance(raw_legs, list):
            continue
        legs: list[Leg] = []
        for raw_leg in raw_legs:
            if not isinstance(raw_leg, dict):
                continue
            legs.append(_map_leg(raw_leg))
            if _leg_has_realtime(raw_leg):
                has_realtime = True
        leg_tuple = tuple(legs)
        journeys.append(
            Journey(journey_id=_journey_id(index, leg_tuple), legs=leg_tuple)
        )
    return tuple(journeys), has_realtime


class SlJourneyError(Exception):
    """A journey-planner failure whose message is safe to log."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SlJourneyProvider:
    """Plans SL public-transport journeys via Journey Planner v2.

    HA-free: it takes an injected aiohttp session. Implements the
    :class:`..providers.base.JourneyProvider` protocol.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        base_url: str = BASE_URL,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")

    async def async_plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        deadline: datetime,
        earliest_departure: datetime | None,
    ) -> JourneyResult:
        """Return up to three journeys arriving by ``deadline``.

        ``earliest_departure`` is accepted for the protocol and used only to drop
        candidates whose first boarding is before it; the arrival search itself
        is anchored on ``deadline``. Any failure yields ``status="error"`` with
        no journeys (spec §11.2).
        """
        fetched_at = datetime.now(UTC)
        params = build_trip_params(origin, destination, deadline)
        try:
            payload = await self._request(params)
        except SlJourneyError as err:
            _LOGGER.warning("SL journey search failed: %s", err.code)
            return JourneyResult(
                status="error",
                journeys=(),
                fetched_at=fetched_at,
                has_realtime=False,
                error_code=err.code,
            )

        if "journeys" not in payload:
            # Error-shaped body: ``journeys`` absent, only ``systemMessages``.
            _LOGGER.warning("SL journey search returned an error response")
            return JourneyResult(
                status="error",
                journeys=(),
                fetched_at=fetched_at,
                has_realtime=False,
                error_code=ERROR_RESPONSE,
            )

        journeys, has_realtime = map_journeys(payload)
        if earliest_departure is not None:
            journeys = tuple(
                journey
                for journey in journeys
                if _reachable_from(journey, earliest_departure)
            )

        return JourneyResult(
            status="ok" if journeys else "empty",
            journeys=journeys,
            fetched_at=fetched_at,
            has_realtime=has_realtime,
            error_code=None,
        )

    async def _request(self, params: dict[str, str]) -> dict[str, Any]:
        """Perform the GET and return the decoded JSON body.

        Raises :class:`SlJourneyError` on timeout, connection error, ``429`` and
        other HTTP errors, or an undecodable body.
        """
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        url = f"{self._base_url}{TRIPS_PATH}"
        try:
            async with self._session.get(
                url, params=params, timeout=timeout
            ) as response:
                if response.status == 429:
                    retry_after = response.headers.get("Retry-After")
                    _LOGGER.warning(
                        "SL journey search rate limited (Retry-After=%s)",
                        retry_after,
                    )
                    raise SlJourneyError(ERROR_RATE_LIMITED, "rate limited (429)")
                if response.status >= 400:
                    raise SlJourneyError(
                        ERROR_HTTP, f"HTTP {response.status} from journey planner"
                    )
                try:
                    data = await response.json(content_type=None)
                except (aiohttp.ClientError, ValueError) as err:
                    raise SlJourneyError(
                        ERROR_DECODE, "could not decode journey response"
                    ) from err
        except TimeoutError as err:
            raise SlJourneyError(
                ERROR_TIMEOUT, "timeout contacting journey planner"
            ) from err
        except aiohttp.ClientError:
            # aiohttp may embed the full URL in the error text; never relay it.
            raise SlJourneyError(
                ERROR_CONNECTION, "connection error contacting journey planner"
            ) from None

        if not isinstance(data, dict):
            raise SlJourneyError(ERROR_DECODE, "unexpected journey response shape")
        return data


def _first_departure(journey: Journey) -> datetime | None:
    """The earliest known departure of a journey (realtime estimate preferred)."""
    for leg in journey.legs:
        for candidate in (leg.estimated_departure, leg.planned_departure):
            if candidate is not None:
                return candidate
    return None


def _reachable_from(journey: Journey, earliest_departure: datetime) -> bool:
    """Whether a journey's first departure is at or after ``earliest_departure``.

    Journeys with no known departure time are kept (the caller cannot prove them
    unreachable).
    """
    first = _first_departure(journey)
    return first is None or first >= earliest_departure
