"""Tests for Family Departures diagnostics and repairs (spec §15, T19).

These exercise two things:

* the config-entry diagnostics export contains no ICS URL, token, coordinate or
  schedule summary text - everything identifying is redacted (spec §15,
  review-privacy-security);
* the coordinator raises and clears the three Repairs issues (invalid source,
  missing destination, repeated fetch failures) as those conditions appear and
  resolve (spec §15).

Setup mirrors ``test_entities``/``test_coordinator``: a full config entry with
the coordinator's provider factories patched to deterministic fakes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from custom_components.family_departures import coordinator as coordinator_mod
from custom_components.family_departures.const import (
    DATA_HOME_LAT,
    DATA_HOME_LON,
    DATA_HOUSEHOLD_NAME,
    DATA_ICS_URLS,
    DOMAIN,
    FETCH_FAILURE_THRESHOLD,
    ISSUE_INVALID_SOURCE,
    ISSUE_MISSING_DESTINATION,
    ISSUE_REPEATED_FETCH_FAILURE,
    OPT_PROFILES,
)
from custom_components.family_departures.coordinator import ProviderFactories
from custom_components.family_departures.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.family_departures.models import (
    DurationResult,
    Journey,
    JourneyResult,
    Leg,
    ProfileConfig,
    ScheduleEvent,
    ScheduleResult,
)
from custom_components.family_departures.timeutil import combine_local
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

# Generic home coords (no real location). The "secret" URL carries a token-like
# query string so the redaction assertions have something to catch.
HOME = (59.33, 18.06)
DEST = (59.41, 18.12)
SECRET_ICS_URL = "https://example.test/calendar?token=FAKE-NOT-A-REAL-TOKEN-123"
CHILD_SUMMARY = "Lektion IDRO1000X Kid A-klassen"
NOW = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)
TODAY = date(2026, 10, 20)


@pytest.fixture(autouse=True)
def _frozen_now():
    with freeze_time(NOW):
        yield


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSchedule:
    """Returns a sequence of results, repeating the last one when exhausted."""

    def __init__(self, *results: ScheduleResult) -> None:
        self._results = list(results)
        self.calls = 0

    async def async_get_day(self, d: date) -> ScheduleResult:
        index = min(self.calls, len(self._results) - 1)
        self.calls += 1
        return self._results[index]


class _FakeJourney:
    def __init__(self, result: JourneyResult) -> None:
        self.result = result

    async def async_plan(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        deadline: datetime,
        earliest_departure: datetime | None,
    ) -> JourneyResult:
        return self.result


class _FakeCar:
    def __init__(self, result: DurationResult) -> None:
        self.result = result

    async def async_get_duration(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        realtime: bool,
        time_delta: timedelta | None,
    ) -> DurationResult:
        return self.result


def _ok_schedule(
    summary: str = CHILD_SUMMARY, hour: int = 8, source_id: str = "kid_a_destination"
) -> ScheduleResult:
    start = combine_local(TODAY, _time(hour, 20))
    return ScheduleResult(
        status="ok",
        events=(
            ScheduleEvent(
                uid="e1",
                summary=summary,
                start=start,
                end=start + timedelta(minutes=50),
                source_id=source_id,
            ),
        ),
        fetched_at=NOW,
    )


def _error_schedule() -> ScheduleResult:
    return ScheduleResult(
        status="error",
        events=(),
        fetched_at=NOW,
        error_code="http_error",
        stale=True,
    )


def _transit_result() -> JourneyResult:
    dep = combine_local(TODAY, _time(7, 30))
    arr = combine_local(TODAY, _time(8, 0))
    return JourneyResult(
        status="ok",
        journeys=(
            Journey(
                journey_id="trip-1",
                legs=(
                    Leg(
                        kind="transit",
                        line="17",
                        from_stop="A",
                        to_stop="B",
                        planned_departure=dep,
                        planned_arrival=arr,
                    ),
                ),
            ),
        ),
        fetched_at=NOW,
    )


def _time(h: int, m: int):
    from datetime import time as _t

    return _t(h, m)


def _profile_dict(
    profile_id: str,
    *,
    default_mode: str = "public_transport",
    dest: tuple[float, float] = DEST,
) -> dict[str, Any]:
    return {
        "id": profile_id,
        "name": f"{profile_id.title()} Realname",
        "source_type": "ics",
        "calendar_entity_id": None,
        "exclude_patterns": [],
        "include_patterns": [],
        "destination_id": f"{profile_id}_destination",
        "dest_lat": dest[0],
        "dest_lon": dest[1],
        "default_mode": default_mode,
        "static_minutes": None,
        "static_label": None,
        "weather_adjust": False,
        "car_fallback_minutes": 18,
        "arrival": 5,
        "departure": 5,
        "boarding": 2,
        "parking_and_walk": 3,
        "min_transfer": 5,
        "weekday_mask": ["0", "1", "2", "3", "4"],
        "packing_rules": [{"id": "r1", "match": "IDRO", "item": "Gympakläder"}],
        "person_entity_id": None,
        "notifications_enabled": False,
        "evening_notice_enabled": True,
        "change_threshold_minutes": 3,
        "quiet_start": "22:00:00",
        "quiet_end": "06:00:00",
        "scripts": {},
    }


def _make_entry(
    profiles: Mapping[str, dict[str, Any]],
    *,
    ics_urls: Mapping[str, str] | None = None,
) -> MockConfigEntry:
    if ics_urls is None:
        ics_urls = {pid: SECRET_ICS_URL for pid in profiles}
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        title="Familjen",
        data={
            DATA_HOUSEHOLD_NAME: "Familjen",
            DATA_HOME_LAT: HOME[0],
            DATA_HOME_LON: HOME[1],
            DATA_ICS_URLS: dict(ics_urls),
        },
        options={OPT_PROFILES: dict(profiles)},
    )


def _patch_factories(
    schedules: Mapping[str, _FakeSchedule],
    *,
    journey: _FakeJourney | None = None,
    car: _FakeCar | None = None,
):
    journey = journey or _FakeJourney(_transit_result())
    car = car or _FakeCar(
        DurationResult(minutes=20.0, fetched_at=NOW, source="waze", quality="realtime")
    )

    def make_schedule(profile: ProfileConfig, ics_url: str | None) -> _FakeSchedule:
        # Mirror the production factory: an ICS profile with no URL is not
        # buildable, which the coordinator turns into an invalid-source issue.
        if profile.source_type == "ics" and not ics_url:
            raise ValueError(f"profile {profile.id} has no ICS URL configured")
        return schedules[profile.id]

    def _factory(hass: HomeAssistant) -> ProviderFactories:
        return ProviderFactories(
            schedule=make_schedule,
            journey=lambda: journey,
            car=lambda: car,
        )

    return patch.object(coordinator_mod, "_default_factories", _factory)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Diagnostics redaction
# ---------------------------------------------------------------------------


async def test_diagnostics_redacts_url_token_coords_and_summary(
    hass: HomeAssistant,
) -> None:
    """The diagnostics dump leaks no URL, token, coordinate or summary (spec §15)."""
    profiles = {"kid_a": _profile_dict("kid_a")}
    entry = _make_entry(profiles)
    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}

    with _patch_factories(schedules):
        await _setup(hass, entry)
        diag = await async_get_config_entry_diagnostics(hass, entry)

    # Serialise and scan the whole payload: nothing identifying may survive.
    blob = json.dumps(diag)
    assert SECRET_ICS_URL not in blob
    assert "FAKE-NOT-A-REAL-TOKEN-123" not in blob
    assert "example.test" not in blob
    assert "token" not in blob
    # No coordinate values (home or destination) in any form.
    for coord in (HOME[0], HOME[1], DEST[0], DEST[1]):
        assert str(coord) not in blob
    # No schedule summary text / child name.
    assert "IDRO1000X" not in blob
    assert "Realname" not in blob
    assert CHILD_SUMMARY not in blob

    # But the operational figures the spec wants ARE present.
    assert diag["source_health"]["kid_a_destination"]["external_calls"] >= 1
    assert diag["source_health"]["kid_a_destination"]["last_success"] is not None
    profile_diag = diag["profiles"][0]
    assert profile_diag["id"] == "kid_a"
    assert profile_diag["schedule_status"] == "ok"
    assert profile_diag["plan"]["mode"] == "public_transport"


async def test_diagnostics_reports_source_error_code_without_content(
    hass: HomeAssistant,
) -> None:
    """A failing source shows its error code and failure count, no content."""
    profiles = {"kid_a": _profile_dict("kid_a")}
    entry = _make_entry(profiles)
    schedules = {"kid_a": _FakeSchedule(_error_schedule())}

    with _patch_factories(schedules):
        await _setup(hass, entry)
        diag = await async_get_config_entry_diagnostics(hass, entry)

    health = diag["source_health"]["kid_a_destination"]
    assert health["last_error_code"] == "http_error"
    assert health["consecutive_failures"] == 1
    assert health["last_success"] is None


# ---------------------------------------------------------------------------
# Repairs issues
# ---------------------------------------------------------------------------


async def test_repeated_fetch_failures_raise_and_clear_issue(
    hass: HomeAssistant,
) -> None:
    """A source failing >= threshold times raises then clears a repair (spec §15)."""
    profiles = {"kid_a": _profile_dict("kid_a")}
    entry = _make_entry(profiles)
    # First an OK fetch (setup), then enough errors to cross the threshold.
    errors = [_error_schedule() for _ in range(FETCH_FAILURE_THRESHOLD)]
    schedule = _FakeSchedule(_ok_schedule(), *errors, _ok_schedule())

    reg = ir.async_get(hass)
    issue_id = f"{ISSUE_REPEATED_FETCH_FAILURE}_kid_a"

    with _patch_factories({"kid_a": schedule}):
        await _setup(hass, entry)
        coordinator = entry.runtime_data
        # Setup did one successful fetch: no issue yet.
        assert reg.async_get_issue(DOMAIN, issue_id) is None

        # Drive enough error rounds to cross the threshold.
        for _ in range(FETCH_FAILURE_THRESHOLD):
            await coordinator.async_refresh()
        assert reg.async_get_issue(DOMAIN, issue_id) is not None

        # A later success clears it again.
        await coordinator.async_refresh()
        assert reg.async_get_issue(DOMAIN, issue_id) is None


async def test_invalid_source_issue_for_profile_without_url(
    hass: HomeAssistant,
) -> None:
    """A profile with no ICS URL gets an invalid-source repair (spec §15)."""
    profiles = {
        "kid_a": _profile_dict("kid_a"),
        "kid_b": _profile_dict("kid_b"),
    }
    # Kid B has no ICS URL, so its provider cannot be built.
    entry = _make_entry(profiles, ics_urls={"kid_a": SECRET_ICS_URL})
    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}

    reg = ir.async_get(hass)
    with _patch_factories(schedules):
        await _setup(hass, entry)

    # Kid A is fine; Kid B is flagged. One broken source does not hide the other.
    assert reg.async_get_issue(DOMAIN, f"{ISSUE_INVALID_SOURCE}_kid_a") is None
    assert reg.async_get_issue(DOMAIN, f"{ISSUE_INVALID_SOURCE}_kid_b") is not None


async def test_missing_destination_issue_raised_and_cleared(
    hass: HomeAssistant,
) -> None:
    """A public-transport profile with no destination coords is flagged (spec §15)."""
    profiles = {"kid_a": _profile_dict("kid_a", dest=(0.0, 0.0))}
    entry = _make_entry(profiles)
    schedules = {"kid_a": _FakeSchedule(_ok_schedule())}

    reg = ir.async_get(hass)
    issue_id = f"{ISSUE_MISSING_DESTINATION}_kid_a"

    with _patch_factories(schedules):
        await _setup(hass, entry)
        assert reg.async_get_issue(DOMAIN, issue_id) is not None

        # Give the profile a destination and reload: the issue clears.
        fixed = {"kid_a": _profile_dict("kid_a", dest=DEST)}
        hass.config_entries.async_update_entry(entry, options={OPT_PROFILES: fixed})
        await hass.async_block_till_done()
        assert reg.async_get_issue(DOMAIN, issue_id) is None


async def test_static_profile_without_destination_not_flagged(
    hass: HomeAssistant,
) -> None:
    """A static-mode profile needs no coordinates, so no missing-dest issue."""
    profiles = {
        "parent_a": _profile_dict("parent_a", default_mode="static", dest=(0.0, 0.0))
    }
    profiles["parent_a"]["static_minutes"] = 15
    profiles["parent_a"]["static_label"] = "Cykel"
    entry = _make_entry(profiles)
    schedules = {
        "parent_a": _FakeSchedule(_ok_schedule(source_id="parent_a_destination"))
    }

    reg = ir.async_get(hass)
    with _patch_factories(schedules):
        await _setup(hass, entry)

    assert reg.async_get_issue(DOMAIN, f"{ISSUE_MISSING_DESTINATION}_parent_a") is None
