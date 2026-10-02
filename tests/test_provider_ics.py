"""Tests for the ICS parser and fetcher (spec §5.1, §5.2, §11.2, §15).

Parsing is tested against sanitised fixtures. Fetching is tested against a
small fake aiohttp session so we can drive redirect chains, size caps, 304s
and errors precisely without the network.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from custom_components.family_departures.models import ScheduleEvent, SourceFilter
from custom_components.family_departures.providers.ics import (
    ERROR_HTTP,
    ERROR_REDIRECT,
    ERROR_TOO_LARGE,
    MAX_RESPONSE_BYTES,
    IcsFetchError,
    IcsScheduleProvider,
    disappeared_uids,
    parse_ics,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ics"

# A placeholder host; the token path is fictional and matches no secret.
URL = "https://feeds.example.test/rest-api/ical-feed/parent/PLACEHOLDER"
HOST = "feeds.example.test"
SOURCE_ID = "kid_b_schoolsoft"
NOW = datetime(2026, 10, 19, 5, 0, tzinfo=UTC)


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _filter_exclude_lunch() -> SourceFilter:
    return SourceFilter(exclude_patterns=("LUNCH",))


# --------------------------------------------------------------------------
# parse_ics
# --------------------------------------------------------------------------


def test_berlin_tzid_summer_local_time() -> None:
    """A Berlin-TZID lesson in summer resolves to the right Stockholm time."""
    events = parse_ics(
        _read("schoolsoft_sample.ics"),
        date(2026, 10, 19),
        _filter_exclude_lunch(),
        SOURCE_ID,
    )
    # 08:20 Berlin (CEST, +02:00) == 06:20 UTC == 08:20 Stockholm.
    assert events[0].start == datetime(2026, 10, 19, 6, 20, tzinfo=UTC)
    assert events[0].start.astimezone().tzinfo is not None


def test_berlin_tzid_winter_local_time() -> None:
    """After the October DST change the same wall-clock maps to +01:00."""
    events = parse_ics(
        _read("schoolsoft_sample.ics"),
        date(2026, 11, 2),
        _filter_exclude_lunch(),
        SOURCE_ID,
    )
    # 08:20 Berlin (CET, +01:00) == 07:20 UTC.
    assert events[0].start == datetime(2026, 11, 2, 7, 20, tzinfo=UTC)


def test_lunch_excluded_by_default_filter() -> None:
    """The LUNCH entry is filtered out while lessons remain, sorted by start."""
    events = parse_ics(
        _read("schoolsoft_sample.ics"),
        date(2026, 10, 19),
        _filter_exclude_lunch(),
        SOURCE_ID,
    )
    summaries = [e.summary for e in events]
    assert "LUNCH" not in summaries
    assert summaries == ["Lektion MATE1001", "Lektion IDRO1000X"]


def test_all_day_event_dropped() -> None:
    """All-day "Ledig" entries never become schedule events."""
    events = parse_ics(
        _read("floating.ics"),
        date(2026, 10, 19),
        SourceFilter(),
        SOURCE_ID,
    )
    assert [e.summary for e in events] == ["Lektion FLYT1001"]


def test_floating_time_uses_source_timezone() -> None:
    """A floating DTSTART is interpreted in X-WR-TIMEZONE (Stockholm here)."""
    events = parse_ics(
        _read("floating.ics"),
        date(2026, 10, 19),
        SourceFilter(),
        SOURCE_ID,
    )
    # 08:20 Stockholm (CEST, +02:00) == 06:20 UTC.
    assert events[0].start == datetime(2026, 10, 19, 6, 20, tzinfo=UTC)


def test_rrule_exdate_and_recurrence_id() -> None:
    """RRULE expands; EXDATE drops an occurrence; RECURRENCE-ID moves one."""
    f = SourceFilter()

    # First occurrence present.
    first = parse_ics(_read("rrule_exdate.ics"), date(2026, 10, 19), f, SOURCE_ID)
    assert len(first) == 1
    assert first[0].start == datetime(2026, 10, 19, 6, 20, tzinfo=UTC)

    # EXDATE removes the 2026-10-26 occurrence entirely.
    excluded = parse_ics(_read("rrule_exdate.ics"), date(2026, 10, 26), f, SOURCE_ID)
    assert excluded == ()

    # RECURRENCE-ID moves 2026-11-02 from 08:20 to 10:00 Berlin (CET).
    moved = parse_ics(_read("rrule_exdate.ics"), date(2026, 11, 2), f, SOURCE_ID)
    assert len(moved) == 1
    assert moved[0].start == datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert "OMFLYTTAD" in moved[0].summary


def test_include_patterns_filter_case_insensitively() -> None:
    """include_patterns keep only matching summaries, ignoring case."""
    events = parse_ics(
        _read("schoolsoft_sample.ics"),
        date(2026, 10, 19),
        SourceFilter(include_patterns=("idro",)),
        SOURCE_ID,
    )
    assert [e.summary for e in events] == ["Lektion IDRO1000X"]


def test_parse_invalid_data_raises() -> None:
    from ical.exceptions import CalendarError

    with pytest.raises(CalendarError):
        parse_ics(b"this is not a calendar", date(2026, 10, 19), SourceFilter(), "x")


# --------------------------------------------------------------------------
# disappeared_uids
# --------------------------------------------------------------------------


def test_disappeared_uids() -> None:
    def ev(uid: str) -> ScheduleEvent:
        return ScheduleEvent(
            uid=uid,
            summary="Lektion",
            start=datetime(2026, 10, 19, 6, 20, tzinfo=UTC),
            end=datetime(2026, 10, 19, 7, 0, tzinfo=UTC),
            source_id=SOURCE_ID,
        )

    previous = (ev("a"), ev("b"), ev("c"))
    current = (ev("a"), ev("c"))
    assert disappeared_uids(previous, current) == frozenset({"b"})
    assert disappeared_uids(current, current) == frozenset()


# --------------------------------------------------------------------------
# Fake aiohttp session for fetcher tests
# --------------------------------------------------------------------------


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for i in range(0, len(self._body), size):
            yield self._body[i : i + size]


class _FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.content = _FakeContent(body)
        self.released = False

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.release()

    def release(self) -> None:
        self.released = True


class _FakeSession:
    """Returns queued responses in order; records requested headers."""

    def __init__(self, responses: list[_FakeResponse | Exception]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: object = None,
        allow_redirects: bool = True,
    ) -> _FakeResponse:
        self.calls.append((url, dict(headers or {})))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _provider(session: _FakeSession) -> IcsScheduleProvider:
    return IcsScheduleProvider(session, URL, _filter_exclude_lunch(), SOURCE_ID)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# async_get_day / async_fetch
# --------------------------------------------------------------------------


async def test_fetch_ok_returns_events() -> None:
    body = _read("schoolsoft_sample.ics")
    session = _FakeSession([_FakeResponse(200, body)])
    provider = _provider(session)

    result = await provider.async_get_day(date(2026, 10, 19), now=NOW)

    assert result.status == "ok"
    assert [e.summary for e in result.events] == [
        "Lektion MATE1001",
        "Lektion IDRO1000X",
    ]
    assert result.stale is False
    assert result.content_hash is not None


async def test_unchanged_hash_skips_reparse() -> None:
    body = _read("schoolsoft_sample.ics")
    session = _FakeSession([_FakeResponse(200, body), _FakeResponse(200, body)])
    provider = _provider(session)

    raw1, changed1, _ = await provider.async_fetch()
    raw2, changed2, _ = await provider.async_fetch()

    assert changed1 is True
    assert changed2 is False
    assert raw1 == raw2


async def test_conditional_request_sends_validators() -> None:
    body = _read("schoolsoft_sample.ics")
    first = _FakeResponse(
        200, body, {"ETag": '"abc"', "Last-Modified": "Wed, 01 Oct 2026 05:00:00 GMT"}
    )
    second = _FakeResponse(304, b"", {})
    session = _FakeSession([first, second])
    provider = _provider(session)

    await provider.async_fetch()
    _, changed, _ = await provider.async_fetch()

    assert changed is False
    # Second request carried the conditional headers.
    _, headers = session.calls[1]
    assert headers.get("If-None-Match") == '"abc"'
    assert headers.get("If-Modified-Since") == "Wed, 01 Oct 2026 05:00:00 GMT"


async def test_http_500_returns_error_with_stale_cache() -> None:
    body = _read("schoolsoft_sample.ics")
    session = _FakeSession([_FakeResponse(200, body), _FakeResponse(500, b"oops")])
    provider = _provider(session)

    ok = await provider.async_get_day(date(2026, 10, 19), now=NOW)
    assert ok.status == "ok"

    err = await provider.async_get_day(date(2026, 10, 19), now=NOW)
    assert err.status == "error"
    assert err.error_code == ERROR_HTTP
    assert err.stale is True
    # Stale cache still yields the previously parsed events.
    assert [e.summary for e in err.events] == [
        "Lektion MATE1001",
        "Lektion IDRO1000X",
    ]


async def test_oversized_response_rejected_by_content_length() -> None:
    session = _FakeSession(
        [_FakeResponse(200, b"x", {"Content-Length": str(MAX_RESPONSE_BYTES + 1)})]
    )
    provider = _provider(session)
    with pytest.raises(IcsFetchError) as exc:
        await provider.async_fetch()
    assert exc.value.code == ERROR_TOO_LARGE


async def test_oversized_response_rejected_while_streaming() -> None:
    big = b"a" * (MAX_RESPONSE_BYTES + 1024)
    session = _FakeSession([_FakeResponse(200, big)])
    provider = _provider(session)
    with pytest.raises(IcsFetchError) as exc:
        await provider.async_fetch()
    assert exc.value.code == ERROR_TOO_LARGE


async def test_cross_host_redirect_refused() -> None:
    session = _FakeSession(
        [_FakeResponse(302, b"", {"Location": "https://evil.example.test/elsewhere"})]
    )
    provider = _provider(session)
    with pytest.raises(IcsFetchError) as exc:
        await provider.async_fetch()
    assert exc.value.code == ERROR_REDIRECT


async def test_same_host_redirect_followed() -> None:
    body = _read("schoolsoft_sample.ics")
    session = _FakeSession(
        [
            _FakeResponse(302, b"", {"Location": "/moved/feed.ics"}),
            _FakeResponse(200, body),
        ]
    )
    provider = _provider(session)
    raw, changed, _ = await provider.async_fetch()
    assert changed is True
    assert raw == body


async def test_timeout_returns_error_result() -> None:
    session = _FakeSession([TimeoutError()])
    provider = _provider(session)
    result = await provider.async_get_day(date(2026, 10, 19), now=NOW)
    assert result.status == "error"
    assert result.stale is True
    # No cache yet, so no events.
    assert result.events == ()


async def test_fetch_log_contains_no_url_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The token path must never appear in logs; only the host may (spec §15)."""
    import aiohttp

    session = _FakeSession([aiohttp.ClientError("boom " + URL)])
    provider = _provider(session)
    with caplog.at_level(logging.WARNING):
        result = await provider.async_get_day(date(2026, 10, 19), now=NOW)

    assert result.status == "error"
    log_text = caplog.text
    assert "PLACEHOLDER" not in log_text
    assert "ical-feed/parent" not in log_text
    assert HOST in log_text
