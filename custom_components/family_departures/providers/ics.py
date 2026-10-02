"""ICS schedule parser and fetcher (spec §5.1, §5.2, §11.2, §15).

Two concerns live here:

* :func:`parse_ics` is a *pure* function (no ``homeassistant``, no network, no
  ``now`` dependency). It expands recurrences with the maintained ``ical``
  library, honours ``TZID``/``VTIMEZONE``, drops all-day events and applies
  case-insensitive include/exclude filters. The parse of the ~190 kB SchoolSoft
  feed is CPU-bound, so the caller runs it in the executor.
* :class:`IcsScheduleProvider` fetches the feed over an injected aiohttp
  session with a bounded timeout, a response-size cap and same-host redirect
  validation. It uses conditional requests (ETag/Last-Modified) when offered
  and otherwise compares a SHA-256 content hash so SchoolSoft (which sends no
  validators) is not reparsed on every poll.

Privacy (spec §15, review-privacy-security): the feed URL carries a personal
token. It must never reach logs or exceptions. Only the host is ever logged.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from ical.calendar_stream import IcsCalendarStream
from ical.exceptions import CalendarError

from ..models import ScheduleEvent, ScheduleResult, SourceFilter
from ..timeutil import TZ, local_day_bounds

_LOGGER = logging.getLogger(__name__)

# Fetch limits (spec §11.2, §15).
FETCH_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB

# Error codes surfaced on ``ScheduleResult`` so the coordinator/dashboard can
# distinguish failure modes without exposing details.
ERROR_TIMEOUT = "timeout"
ERROR_HTTP = "http_error"
ERROR_CONNECTION = "connection_error"
ERROR_TOO_LARGE = "response_too_large"
ERROR_REDIRECT = "untrusted_redirect"
ERROR_PARSE = "parse_error"


def _host_of(url: str) -> str:
    """Return the host of a URL for safe logging (never the token path)."""
    return urlsplit(url).hostname or "<unknown-host>"


def _matches_any(summary: str, patterns: Iterable[str]) -> bool:
    """Case-insensitive substring match of ``summary`` against ``patterns``."""
    folded = summary.casefold()
    return any(pattern.casefold() in folded for pattern in patterns)


def _to_utc(value: datetime, fallback_tz: ZoneInfo) -> datetime:
    """Coerce an event datetime to aware UTC.

    ``ical`` already attaches the ``TZID``/``VTIMEZONE`` zone to timed events.
    A *floating* time comes back naive; spec §5.1 says to interpret it in the
    source's configured zone (``X-WR-TIMEZONE``), defaulting to Stockholm.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        # Floating/ambiguous time: flag it (spec §5.1) and interpret in the
        # source zone. Only the local wall-clock is logged, never event content.
        _LOGGER.debug(
            "Floating ICS time %s interpreted as %s",
            value.strftime("%Y-%m-%dT%H:%M:%S"),
            fallback_tz.key,
        )
        value = value.replace(tzinfo=fallback_tz)
    return value.astimezone(UTC)


def _source_timezone(x_wr_timezone: str | None) -> ZoneInfo:
    """Resolve the fallback zone for floating times (spec §5.1)."""
    if x_wr_timezone:
        try:
            return ZoneInfo(x_wr_timezone)
        except Exception:  # noqa: BLE001 - unknown/invalid zone name
            _LOGGER.debug(
                "Unknown X-WR-TIMEZONE %r; using Europe/Stockholm for floating times",
                x_wr_timezone,
            )
    return TZ


def parse_ics(
    data: bytes,
    d: date,
    source_filter: SourceFilter,
    source_id: str,
) -> tuple[ScheduleEvent, ...]:
    """Parse ``data`` and return the filtered events for local date ``d``.

    Pure and side-effect free. Recurrences (RRULE/RDATE/EXDATE/RECURRENCE-ID)
    are expanded by ``ical`` through ``timeline_tz``; all-day events are
    dropped; include/exclude patterns are applied case-insensitively.

    Raises :class:`ical.exceptions.CalendarError` on unparseable input so the
    fetcher can translate it into a ``status="error"`` result.
    """
    text = data.decode("utf-8", errors="replace")
    calendar = IcsCalendarStream.calendar_from_ics(text)
    fallback_tz = _source_timezone(calendar.x_wr_timezone)

    start_utc, end_utc = local_day_bounds(d)
    # ``overlapping`` wants zone-aware datetimes in the query zone; use the
    # local-day bounds so recurrence expansion matches the local calendar day.
    start_local = start_utc.astimezone(TZ)
    end_local = end_utc.astimezone(TZ)

    exclude = source_filter.exclude_patterns
    include = source_filter.include_patterns

    events: list[ScheduleEvent] = []
    for item in calendar.timeline_tz(TZ).overlapping(start_local, end_local):
        item_start = item.start
        item_end = item.end
        # All-day events have ``date`` (not ``datetime``) bounds; drop them.
        if not isinstance(item_start, datetime) or not isinstance(item_end, datetime):
            continue
        summary = item.summary or ""
        if exclude and _matches_any(summary, exclude):
            continue
        if include and not _matches_any(summary, include):
            continue
        start = _to_utc(item_start, fallback_tz)
        end = _to_utc(item_end, fallback_tz)
        events.append(
            ScheduleEvent(
                uid=item.uid or "",
                summary=summary,
                start=start,
                end=end,
                source_id=source_id,
            )
        )

    events.sort(key=lambda e: e.start)
    return tuple(events)


def disappeared_uids(
    previous: tuple[ScheduleEvent, ...],
    current: tuple[ScheduleEvent, ...],
) -> frozenset[str]:
    """UIDs present in ``previous`` but absent from ``current``.

    SchoolSoft does not emit ``STATUS:CANCELLED``; a cancelled lesson simply
    disappears from the feed (spec §5.2). Schedule selection uses this to treat
    a vanished first lesson as a possible cancellation before departure.
    """
    previous_uids = {e.uid for e in previous if e.uid}
    current_uids = {e.uid for e in current if e.uid}
    return frozenset(previous_uids - current_uids)


class IcsFetchError(Exception):
    """A fetch failure whose message is safe to log (host only, no token)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class _CacheEntry:
    """Last successful fetch, used for conditional requests and stale fallback."""

    content_hash: str
    etag: str | None
    last_modified: str | None
    raw: bytes
    source_modified_at: datetime | None


class IcsScheduleProvider:
    """Fetches and parses one ICS feed. HA-free: it takes an aiohttp session.

    The caller is responsible for running the (CPU-bound) parse in the
    executor via :func:`parse_ics`; :meth:`async_get_day` does the fetch itself
    off the event loop's blocking work and only parses the small per-day slice.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        source_filter: SourceFilter,
        source_id: str,
    ) -> None:
        self._session = session
        self._url = url
        self._filter = source_filter
        self._source_id = source_id
        self._host = _host_of(url)
        self._cache: _CacheEntry | None = None

    @property
    def source_id(self) -> str:
        return self._source_id

    async def async_fetch(self) -> tuple[bytes, bool, datetime | None]:
        """Fetch the feed body.

        Returns ``(raw_bytes, changed, source_modified_at)``. ``changed`` is
        ``False`` when the server reports 304 or the content hash is unchanged,
        letting the caller skip reparsing (spec §5.2).

        Raises :class:`IcsFetchError` on network/HTTP/size/redirect problems;
        the message carries only the host, never the token URL.
        """
        headers: dict[str, str] = {}
        if self._cache is not None:
            if self._cache.etag:
                headers["If-None-Match"] = self._cache.etag
            if self._cache.last_modified:
                headers["If-Modified-Since"] = self._cache.last_modified

        timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
        url = self._url
        # Redirects are followed manually so cross-host targets can be refused
        # (spec §15). Each hop uses ``async with`` so the body is released.
        try:
            for hop in range(6):
                request_headers = headers if hop == 0 else {}
                async with self._session.get(
                    url,
                    headers=request_headers,
                    timeout=timeout,
                    allow_redirects=False,
                ) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        url = self._redirect_target(response)
                        continue

                    if response.status == 304 and self._cache is not None:
                        return (
                            self._cache.raw,
                            False,
                            self._cache.source_modified_at,
                        )

                    if response.status >= 400:
                        raise IcsFetchError(
                            ERROR_HTTP,
                            f"HTTP {response.status} from {self._host}",
                        )

                    raw = await self._read_capped(response)
                    etag = response.headers.get("ETag")
                    last_modified = response.headers.get("Last-Modified")
                    break
            else:
                raise IcsFetchError(
                    ERROR_REDIRECT, f"too many redirects at {self._host}"
                )
        except TimeoutError as err:
            raise IcsFetchError(
                ERROR_TIMEOUT, f"timeout fetching from {self._host}"
            ) from err
        except aiohttp.ClientError:
            # Never include the error text: aiohttp embeds the full URL in it.
            raise IcsFetchError(
                ERROR_CONNECTION, f"connection error to {self._host}"
            ) from None

        content_hash = hashlib.sha256(raw).hexdigest()
        source_modified_at = _parse_http_date(last_modified)

        changed = self._cache is None or self._cache.content_hash != content_hash
        self._cache = _CacheEntry(
            content_hash=content_hash,
            etag=etag,
            last_modified=last_modified,
            raw=raw,
            source_modified_at=source_modified_at,
        )
        return raw, changed, source_modified_at

    def _redirect_target(self, response: aiohttp.ClientResponse) -> str:
        """Validate a redirect ``Location`` and return the absolute target.

        Refuses missing locations and any host other than the origin (§15).
        """
        location = response.headers.get("Location")
        if location is None:
            raise IcsFetchError(
                ERROR_REDIRECT, f"redirect without location at {self._host}"
            )
        target = urlsplit(location)
        if target.hostname is not None and target.hostname != self._host:
            raise IcsFetchError(
                ERROR_REDIRECT,
                f"refused cross-host redirect from {self._host}",
            )
        if target.scheme:
            return location
        return _join_same_host(self._url, location)

    async def _read_capped(self, response: aiohttp.ClientResponse) -> bytes:
        """Read the body, rejecting anything over the size cap (spec §15)."""
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                if int(declared) > MAX_RESPONSE_BYTES:
                    response.release()
                    raise IcsFetchError(
                        ERROR_TOO_LARGE,
                        f"response from {self._host} exceeds size cap",
                    )
            except ValueError:
                pass  # Fall through to streaming cap below.

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                response.release()
                raise IcsFetchError(
                    ERROR_TOO_LARGE,
                    f"response from {self._host} exceeds size cap",
                )
            chunks.append(chunk)
        return b"".join(chunks)

    async def async_get_day(
        self, d: date, *, now: datetime | None = None
    ) -> ScheduleResult:
        """Fetch and parse the schedule for local date ``d``.

        On any fetch error, returns ``status="error"`` with the last cached
        events (if any) and ``stale=True`` (spec §5.4, §11.2). A successful
        fetch with no matching events returns ``status="empty"``.
        """
        fetched_at = now or datetime.now(UTC)
        try:
            raw, _changed, source_modified_at = await self.async_fetch()
        except IcsFetchError as err:
            _LOGGER.warning(
                "ICS fetch failed for source %s (%s): %s",
                self._source_id,
                self._host,
                err.code,
            )
            cached = self._cache
            events: tuple[ScheduleEvent, ...] = ()
            if cached is not None:
                try:
                    events = parse_ics(cached.raw, d, self._filter, self._source_id)
                except CalendarError:
                    events = ()
            return ScheduleResult(
                status="error",
                events=events,
                fetched_at=fetched_at,
                source_modified_at=cached.source_modified_at if cached else None,
                content_hash=cached.content_hash if cached else None,
                error_code=err.code,
                stale=True,
            )

        try:
            events = parse_ics(raw, d, self._filter, self._source_id)
        except CalendarError:
            _LOGGER.warning(
                "ICS parse failed for source %s (%s)", self._source_id, self._host
            )
            return ScheduleResult(
                status="error",
                events=(),
                fetched_at=fetched_at,
                source_modified_at=source_modified_at,
                content_hash=self._cache.content_hash if self._cache else None,
                error_code=ERROR_PARSE,
                stale=True,
            )

        return ScheduleResult(
            status="ok" if events else "empty",
            events=events,
            fetched_at=fetched_at,
            source_modified_at=source_modified_at,
            content_hash=self._cache.content_hash if self._cache else None,
            error_code=None,
            stale=False,
        )


def _join_same_host(base: str, relative: str) -> str:
    """Join a scheme-less redirect target onto the origin, preserving scheme."""
    base_parts = urlsplit(base)
    if relative.startswith("//"):
        return f"{base_parts.scheme}:{relative}"
    if relative.startswith("/"):
        return f"{base_parts.scheme}://{base_parts.netloc}{relative}"
    return f"{base_parts.scheme}://{base_parts.netloc}/{relative}"


def _parse_http_date(value: str | None) -> datetime | None:
    """Parse an HTTP ``Last-Modified`` date into aware UTC, or ``None``."""
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
