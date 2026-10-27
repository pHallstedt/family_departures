"""Household parsing and config-entry v1 → v2 migration (plan §2, §3).

This module owns two pure functions:

* :func:`migrate_v1_to_v2` reshapes a version-1 config entry (one source +
  destination per profile) into the version-2 household shape: a ``locations``
  and ``calendars`` registry plus profiles that reference them through calendar
  links (D1, D2). It moves the secret ICS URL from being keyed by *profile id*
  to being keyed by *calendar id*, deduplicating shared calendars, and it never
  logs a URL value (review-privacy-security, §10).
* :func:`parse_household` turns the v2 ``entry.data``/``entry.options`` into the
  pure :class:`HouseholdConfig` the coordinator plans against. It replaces the
  old ``coordinator._profile_from_options``.

Both are ``homeassistant``-free so they stay unit-testable.

Behaviour parity for step 2: every migrated profile gets exactly one ``main``
calendar link built from its v1 source, so selection, missions, ledgers and
entity ids are unchanged. The richer multi-link shapes land in later steps.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from typing import Any

from .const import DATA_ICS_URLS, DEFAULT_CHANGE_THRESHOLD_MINUTES, OPT_PROFILES
from .models import (
    ALL_COPY_KINDS,
    CalendarLink,
    CalendarSource,
    HouseholdConfig,
    Location,
    Margins,
    PackingRule,
    ProfileConfig,
    SourceFilter,
)

# The main link created for every migrated/single-calendar profile uses this
# fixed link id so its mission slot stays ``morning`` (via ``select_requirement``)
# and extra-mission slots in later steps never collide with it.
MAIN_LINK_ID = "main"


# ---------------------------------------------------------------------------
# v1 → v2 migration (plan §3)
# ---------------------------------------------------------------------------


def _profile_v1_to_v2(
    pid: str,
    raw: Mapping[str, Any],
    *,
    calendars: dict[str, dict[str, Any]],
    locations: dict[str, dict[str, Any]],
    ics_urls_v1: Mapping[str, Any],
    ics_urls_v2: dict[str, Any],
) -> dict[str, Any]:
    """Reshape one v1 profile into v2, registering its calendar and location.

    ``calendars``/``locations``/``ics_urls_v2`` are mutated in place so calendars
    shared by two profiles (same HA entity, or an identical ICS URL) collapse to
    one record with two links (F5 for free, plan §3 step 2). Returns the v2
    profile dict. A profile whose v1 shape is invalid is returned untouched so
    the coordinator reports ``invalid_source`` exactly as before (plan §3 step 5).
    """
    source_type = raw.get("source_type")
    if source_type not in ("ics", "ha_calendar"):
        # Unknown/broken v1 profile: copy untouched, let the coordinator flag it.
        return dict(raw)

    name = str(raw.get("name", pid))

    # 1. Location from the v1 destination coordinates. Reusing the old
    #    destination_id keeps stored plans' ArrivalRequirement.destination_id
    #    meaningful after the upgrade.
    location_id = str(raw.get("destination_id") or f"{pid}_destination")
    if location_id not in locations:
        locations[location_id] = {
            "id": location_id,
            "name": f"{name} \u2013 m\u00e5l",
            "lat": float(raw.get("dest_lat", 0.0) or 0.0),
            "lon": float(raw.get("dest_lon", 0.0) or 0.0),
            "aliases": [],
            "static_minutes": None,
        }

    # 2. Calendar, deduplicated. HA calendars dedup by entity id; ICS feeds dedup
    #    by URL string (compared in memory only, never logged).
    calendar_id = _resolve_calendar_id(
        pid,
        source_type,
        raw,
        calendars=calendars,
        ics_urls_v1=ics_urls_v1,
        ics_urls_v2=ics_urls_v2,
        name=name,
    )

    # 3. Main link from the v1 filter/source.
    link = {
        "link_id": MAIN_LINK_ID,
        "calendar_id": calendar_id,
        "role": "main",
        "label": "",
        "include_patterns": list(raw.get("include_patterns", []) or []),
        "exclude_patterns": list(raw.get("exclude_patterns", []) or []),
        "attendee_mode": "all",
        "location_id": None,
        "mode": None,
        "static_minutes": None,
        "arrival_margin": None,
        "rules": [],
        "match_event_location": False,
        "show_summary": False,
    }

    # 4. Profile fields: keep everything except the v1 source/destination keys,
    #    add the v2 registries references.
    v2: dict[str, Any] = {
        key: value
        for key, value in raw.items()
        if key
        not in (
            "source_type",
            "calendar_entity_id",
            "include_patterns",
            "exclude_patterns",
            "destination_id",
            "dest_lat",
            "dest_lon",
        )
    }
    v2["id"] = pid
    v2["name"] = name
    v2["default_location_id"] = location_id
    v2["calendar_links"] = [link]
    v2.setdefault("copy_to", [])
    v2.setdefault("copy_kinds", sorted(ALL_COPY_KINDS))
    v2.setdefault("lunch_calendar_entity_id", None)
    v2.setdefault("lunch_in_digest", True)
    return v2


def _resolve_calendar_id(
    pid: str,
    source_type: str,
    raw: Mapping[str, Any],
    *,
    calendars: dict[str, dict[str, Any]],
    ics_urls_v1: Mapping[str, Any],
    ics_urls_v2: dict[str, Any],
    name: str,
) -> str:
    """Return the calendar id for a v1 profile, creating/deduplicating it.

    HA calendars dedup by their entity id; ICS feeds dedup by URL. The URL value
    is only compared and moved in memory, never written to a log (§10).
    """
    if source_type == "ha_calendar":
        entity_id = raw.get("calendar_entity_id")
        for cal_id, cal in calendars.items():
            if (
                cal.get("source_type") == "ha_calendar"
                and cal.get("calendar_entity_id") == entity_id
            ):
                return cal_id
        calendar_id = f"{pid}_calendar"
        calendars[calendar_id] = {
            "id": calendar_id,
            "name": f"{name} \u2013 schema",
            "source_type": "ha_calendar",
            "calendar_entity_id": entity_id,
        }
        return calendar_id

    # ICS: dedup by URL string.
    url = ics_urls_v1.get(pid)
    if url:
        # Reuse an existing ICS calendar that already points at this URL.
        for cal_id, cal in calendars.items():
            if cal.get("source_type") == "ics" and ics_urls_v2.get(cal_id) == url:
                return cal_id
    calendar_id = f"{pid}_calendar"
    calendars[calendar_id] = {
        "id": calendar_id,
        "name": f"{name} \u2013 schema",
        "source_type": "ics",
        "calendar_entity_id": None,
    }
    if url:
        ics_urls_v2[calendar_id] = url
    return calendar_id


def migrate_v1_to_v2(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Migrate a v1 config entry's ``data``/``options`` to the v2 shape (§3).

    Returns new ``(data, options)`` dicts; the inputs are not mutated. The
    secret ICS URLs are re-keyed from profile id to calendar id in memory only.
    """
    new_data = dict(data)
    new_options = dict(options)

    ics_urls_v1: Mapping[str, Any] = dict(data.get(DATA_ICS_URLS, {}) or {})
    raw_profiles: Mapping[str, Any] = dict(options.get(OPT_PROFILES, {}) or {})

    calendars: dict[str, dict[str, Any]] = dict(new_options.get("calendars", {}) or {})
    locations: dict[str, dict[str, Any]] = dict(new_options.get("locations", {}) or {})
    # Carry forward URLs already keyed by an existing calendar id (a re-save of
    # one profile when other profiles are already v2); only profile-keyed URLs
    # are re-keyed below.
    ics_urls_v2: dict[str, Any] = {
        key: value for key, value in ics_urls_v1.items() if key in calendars
    }

    v2_profiles: dict[str, Any] = {}
    for pid, raw in raw_profiles.items():
        if not isinstance(raw, Mapping):
            # Preserve non-mapping entries verbatim; the coordinator skips them.
            v2_profiles[str(pid)] = raw
            continue
        v2_profiles[str(pid)] = _profile_v1_to_v2(
            str(pid),
            raw,
            calendars=calendars,
            locations=locations,
            ics_urls_v1=ics_urls_v1,
            ics_urls_v2=ics_urls_v2,
        )

    new_options[OPT_PROFILES] = v2_profiles
    new_options["calendars"] = calendars
    new_options["locations"] = locations
    new_data[DATA_ICS_URLS] = ics_urls_v2
    return new_data, new_options


# ---------------------------------------------------------------------------
# Household parsing (replaces coordinator._profile_from_options)
# ---------------------------------------------------------------------------


def _parse_time_value(value: Any, default: str) -> time:
    if isinstance(value, time):
        return value
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        return time.fromisoformat(default)


def _location_from_raw(raw: Mapping[str, Any]) -> Location:
    static_minutes = raw.get("static_minutes")
    return Location(
        id=str(raw["id"]),
        name=str(raw.get("name", raw["id"])),
        lat=float(raw.get("lat", 0.0) or 0.0),
        lon=float(raw.get("lon", 0.0) or 0.0),
        aliases=tuple(str(a) for a in raw.get("aliases", ()) or ()),
        static_minutes=int(static_minutes) if static_minutes is not None else None,
    )


def _calendar_from_raw(raw: Mapping[str, Any]) -> CalendarSource:
    return CalendarSource(
        id=str(raw["id"]),
        name=str(raw.get("name", raw["id"])),
        source_type=raw["source_type"],
        calendar_entity_id=raw.get("calendar_entity_id"),
    )


def _link_from_raw(raw: Mapping[str, Any]) -> CalendarLink:
    source_filter = SourceFilter(
        exclude_patterns=tuple(raw.get("exclude_patterns", ()) or ()),
        include_patterns=tuple(raw.get("include_patterns", ()) or ()),
    )
    static_minutes = raw.get("static_minutes")
    arrival_margin = raw.get("arrival_margin")
    return CalendarLink(
        link_id=str(raw["link_id"]),
        calendar_id=str(raw["calendar_id"]),
        role=raw.get("role", "extra"),
        label=str(raw.get("label", "") or ""),
        source_filter=source_filter,
        attendee_mode=raw.get("attendee_mode", "all"),
        location_id=raw.get("location_id"),
        mode=raw.get("mode"),
        static_minutes=int(static_minutes) if static_minutes is not None else None,
        arrival_margin=int(arrival_margin) if arrival_margin is not None else None,
        match_event_location=bool(raw.get("match_event_location", False)),
        show_summary=bool(raw.get("show_summary", False)),
    )


def _profile_from_options(profile_id: str, raw: Mapping[str, Any]) -> ProfileConfig:
    """Build a :class:`ProfileConfig` from one stored v2 options profile.

    Mirrors the shape written by the options flow / the migration; the secret
    ICS URL is deliberately not read here (it lives in ``entry.data`` keyed by
    calendar id and reaches the coordinator through the provider factory,
    review-privacy-security).

    A profile still in the v1 shape (it has ``source_type``/``destination_id`` but
    no ``calendar_links``) is one the migration could not reshape, such as an
    invalid ``source_type``. It is rejected so the coordinator flags it
    ``invalid_config`` instead of running it as a profile with no calendar.
    """
    if "calendar_links" not in raw and (
        "source_type" in raw or "destination_id" in raw
    ):
        raise ValueError("profile is still in the unmigrated v1 shape")
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
    links = tuple(
        _link_from_raw(link)
        for link in raw.get("calendar_links", []) or []
        if isinstance(link, Mapping)
    )
    copy_kinds = raw.get("copy_kinds")
    return ProfileConfig(
        id=profile_id,
        name=str(raw["name"]),
        default_location_id=str(
            raw.get("default_location_id") or f"{profile_id}_destination"
        ),
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
        calendar_links=links,
        copy_to=tuple(str(p) for p in raw.get("copy_to", ()) or ()),
        copy_kinds=(
            frozenset(str(k) for k in copy_kinds)
            if copy_kinds is not None
            else ALL_COPY_KINDS
        ),
        lunch_calendar_entity_id=raw.get("lunch_calendar_entity_id"),
        lunch_in_digest=bool(raw.get("lunch_in_digest", True)),
        evening_notice_enabled=bool(raw.get("evening_notice_enabled", True)),
    )


def parse_household(
    data: Mapping[str, Any], options: Mapping[str, Any]
) -> HouseholdConfig:
    """Parse the v2 ``entry.data``/``entry.options`` into a HouseholdConfig.

    Parsing is isolated per profile: a profile whose options cannot be built
    into a :class:`ProfileConfig` is skipped (left out of ``profiles``) so the
    coordinator flags only that profile ``invalid_config`` and keeps planning
    for the rest, matching v1's per-profile fault isolation (spec §15). Invalid
    locations or calendars are likewise skipped rather than failing the entry.
    """
    from .const import DATA_HOME_LAT, DATA_HOME_LON

    home = (
        float(data.get(DATA_HOME_LAT, 0.0) or 0.0),
        float(data.get(DATA_HOME_LON, 0.0) or 0.0),
    )
    locations = {
        str(loc_id): _location_from_raw({**raw, "id": raw.get("id", loc_id)})
        for loc_id, raw in (options.get("locations", {}) or {}).items()
        if isinstance(raw, Mapping)
    }
    calendars = {
        str(cal_id): _calendar_from_raw({**raw, "id": raw.get("id", cal_id)})
        for cal_id, raw in (options.get("calendars", {}) or {}).items()
        if isinstance(raw, Mapping)
    }
    profiles: dict[str, ProfileConfig] = {}
    for pid, raw in (options.get(OPT_PROFILES, {}) or {}).items():
        if not isinstance(raw, Mapping):
            continue
        try:
            profiles[str(pid)] = _profile_from_options(str(pid), raw)
        except KeyError, ValueError, TypeError:
            # Isolate the failure: skip this profile so the coordinator flags
            # only it ``invalid_config`` while the others keep planning. The
            # exception is not logged here because it can embed coordinates or
            # other profile fields (§10); the coordinator logs the profile id.
            continue
    return HouseholdConfig(
        home=home,
        locations=locations,
        calendars=calendars,
        profiles=profiles,
    )


__all__ = [
    "MAIN_LINK_ID",
    "migrate_v1_to_v2",
    "parse_household",
]
