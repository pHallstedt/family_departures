#!/usr/bin/env python3
"""Report the *structure* of an ICS feed without exposing its content (H3).

Usage::

    uv run python scripts/inspect_ics.py <url>

This is a privacy-preserving diagnostic for a parent to run on a personal
Vklass/SchoolSoft feed (spec §5.1, §5.2, §15). It prints only aggregate
structure: property counts, time-zone identifiers, SUMMARY *prefixes* with
counts, the distribution of first-of-day start times and whether the server
offers caching validators.

It deliberately prints no event titles, descriptions, teachers, rooms, UIDs,
coordinates or the URL token. The output is safe to commit to
``docs/sources/vklass.md``.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date, datetime
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ical.calendar_stream import IcsCalendarStream


def _host_of(url: str) -> str:
    return urlsplit(url).hostname or "<unknown-host>"


def _summary_prefix(summary: str) -> str:
    """A coarse, non-identifying prefix of a SUMMARY (first token only)."""
    summary = summary.strip()
    if not summary:
        return "<empty>"
    return summary.split()[0]


def _fetch(url: str) -> tuple[bytes, dict[str, str]]:
    request = Request(url, headers={"User-Agent": "family_departures-inspect/1"})
    with urlopen(request, timeout=15) as response:  # noqa: S310 - user-supplied feed
        body = response.read()
        headers = {k.lower(): v for k, v in response.headers.items()}
    return body, headers


def inspect(url: str) -> str:
    host = _host_of(url)
    body, headers = _fetch(url)
    calendar = IcsCalendarStream.calendar_from_ics(body.decode("utf-8", "replace"))

    lines: list[str] = []
    lines.append(f"host: {host}")
    lines.append(f"bytes: {len(body)}")
    lines.append(f"prodid: {calendar.prodid}")
    lines.append(f"x-wr-timezone: {calendar.x_wr_timezone or '<none>'}")

    tz_ids = sorted({tz.tz_id for tz in calendar.timezones})
    lines.append(f"vtimezone tzids: {tz_ids or '<none>'}")

    extras = sorted(f"{p.name}={p.value}" for p in calendar.extras)
    lines.append(f"calendar extras: {extras or '<none>'}")

    lines.append("")
    lines.append("caching headers:")
    for key in ("etag", "last-modified", "cache-control", "x-published-ttl"):
        lines.append(f"  {key}: {headers.get(key, '<none>')}")

    events = calendar.events
    lines.append("")
    lines.append(f"event count: {len(events)}")
    with_rrule = sum(1 for e in events if e.rrule is not None)
    with_recurrence_id = sum(1 for e in events if e.recurrence_id is not None)
    all_day = sum(
        1
        for e in events
        if isinstance(e.start, date) and not isinstance(e.start, datetime)
    )
    lines.append(f"  with RRULE: {with_rrule}")
    lines.append(f"  with RECURRENCE-ID: {with_recurrence_id}")
    lines.append(f"  all-day: {all_day}")

    prefixes = Counter(_summary_prefix(e.summary or "") for e in events)
    lines.append("")
    lines.append("summary prefixes (count):")
    for prefix, count in sorted(prefixes.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {prefix}: {count}")

    # First-of-day start-time distribution (local wall-clock hour:minute only).
    first_by_day: dict[date, datetime] = {}
    for event in events:
        start = event.start
        if not isinstance(start, datetime):
            continue
        day = start.date()
        if day not in first_by_day or start < first_by_day[day]:
            first_by_day[day] = start
    first_times = Counter(dt.strftime("%H:%M") for dt in first_by_day.values())
    lines.append("")
    lines.append("first-of-day start times (count):")
    for hhmm, count in sorted(first_times.items()):
        lines.append(f"  {hhmm}: {count}")

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: inspect_ics.py <url>", file=sys.stderr)
        return 2
    print(inspect(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
