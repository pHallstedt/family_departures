"""Config flow and options flow for Family Departures (spec §2, §4.1-§4.3, §5.5).

The config flow creates the single household entry: a name and the home
coordinates. All ongoing configuration happens in the options flow, which is a
menu offering:

* **locations** and **calendars** - household registries (plan D1). A location
  is a named, mapped place with aliases; a calendar is an ICS link or a Home
  Assistant calendar entity, with the persons (profiles) it is linked to. A
  location is protected from removal while a profile or link uses it; removing a
  calendar removes its links and its secret URL.
* **add/edit/remove profile** - walks through the person, destination (a saved
  or an inline new location) and travel mode, the linked calendars (one main,
  any number of activity calendars, or none), one settings step per link
  (filters, attendee mode, defaults, rules), margins and weekday mask, packing
  rules, quiet hours, copies, lunch and channel scripts, and ends on a read-only
  test calculation that shows the plan for both car and transit (spec §4.3: the
  config flow must not hide SL settings for car-default profiles). A profile's id
  is a stable slug derived from its name at creation; renaming keeps the id.
  Removing a profile purges its device and entities and stops other profiles
  copying notices to it; the shared locations and calendars stay.
* **settings** - the global dry-run flag and the optional test-recipient script
  (one ``script.*`` entity; during development every notification goes only
  there - spec Environment facts, T18).

Secret handling (review-privacy-security, spec §4.2): the ICS URL is entered as
a password-type field and stored only in ``entry.data`` under
``DATA_ICS_URLS``, keyed by calendar id. It is never placed in ``entry.options``,
in a form description/title, or in an error message. Everything else lives in
``entry.options``.

Persistent profile settings change only here, never through entities (spec
§4.1); the entities built in T14 only write per-day overrides and the
notification switch. Saving the options triggers a reload so the coordinator
picks up the new configuration without stale timers.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import slugify

from .calendar_links import (
    RulesError,
    filter_for_link,
    format_rules_text,
    parse_rules_text,
)
from .const import (
    DATA_HOME_LAT,
    DATA_HOME_LON,
    DATA_HOUSEHOLD_NAME,
    DATA_ICS_URLS,
    DEFAULT_ARRIVAL_MARGIN_ADULT,
    DEFAULT_ARRIVAL_MARGIN_CHILD,
    DEFAULT_BOARDING_MARGIN,
    DEFAULT_CHANGE_THRESHOLD_MINUTES,
    DEFAULT_DEPARTURE_MARGIN,
    DEFAULT_DRY_RUN,
    DEFAULT_MIN_TRANSFER_MARGIN,
    DEFAULT_PARKING_WALK_MARGIN,
    DOMAIN,
    MODES,
    NOTIFICATION_CHANNELS,
    OPT_DRY_RUN,
    OPT_PROFILES,
    OPT_TEST_RECIPIENT_SCRIPT,
)
from .household import _link_from_raw, _location_from_raw, _profile_from_options
from .models import (
    ALL_COPY_KINDS,
    ArrivalRequirement,
    CalendarLink,
    DurationResult,
    Location,
    Mode,
    SourceFilter,
)
from .planner import plan_fixed
from .providers.ics import IcsFetchError, IcsScheduleProvider, parse_ics
from .timeutil import TZ, combine_local, make_mission_id

_LOGGER = logging.getLogger(__name__)

SOURCE_TYPES = ("ics", "ha_calendar")
ATTENDEE_MODES = ("all", "tagged", "tagged_or_untagged")
# Select values meaning "use the profile's own default" / "a calendar created in
# this form" (never stored).
# The values contain a hyphen so they can never equal a slugified registry id.
LINK_DEFAULT = "profile-default"
NEW_CALENDAR = "new-calendar"
WEEKDAY_OPTIONS = ("0", "1", "2", "3", "4", "5", "6")
WEEKDAY_LABELS = {
    "0": "Mån",
    "1": "Tis",
    "2": "Ons",
    "3": "Tor",
    "4": "Fre",
    "5": "Lör",
    "6": "Sön",
}
DEFAULT_WEEKDAY_MASK = ("0", "1", "2", "3", "4")
# How many future days to scan when previewing packing-rule matches (§4.1 pt 4).
PACKING_PREVIEW_DAYS = 14
PACKING_PREVIEW_LIMIT = 5
# The travel duration used in the config-flow test calculation. The flow makes
# no network calls (that is the coordinator's job, T13); a representative value
# demonstrates both car and transit plans for a car-default profile (spec §4.3).
_PREVIEW_TRAVEL_MINUTES = 20.0


class FamilyDeparturesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial household config flow."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the single household entry: name and home coordinates."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            location = user_input["home_location"]
            return self.async_create_entry(
                title=user_input[DATA_HOUSEHOLD_NAME],
                data={
                    DATA_HOUSEHOLD_NAME: user_input[DATA_HOUSEHOLD_NAME],
                    DATA_HOME_LAT: location["latitude"],
                    DATA_HOME_LON: location["longitude"],
                    DATA_ICS_URLS: {},
                },
                options={OPT_PROFILES: {}},
            )

        schema = vol.Schema(
            {
                vol.Required(
                    DATA_HOUSEHOLD_NAME, default="Familjen"
                ): selector.TextSelector(),
                # The frontend's compute-initial-ha-form-data throws for a
                # `location` selector that has neither a non-null suggested
                # value nor a default, crashing the dialog before it renders.
                # Seed it with the HA instance's configured home coordinates so
                # the pin starts there and the user can still drag it.
                vol.Required(
                    "home_location",
                    description={
                        "suggested_value": {
                            "latitude": self.hass.config.latitude,
                            "longitude": self.hass.config.longitude,
                        }
                    },
                ): selector.LocationSelector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return FamilyDeparturesOptionsFlow()


class FamilyDeparturesOptionsFlow(OptionsFlow):
    """Options flow: profiles, the location and calendar registries, settings.

    Locations and calendars are household resources (plan D1) managed from their
    own submenus; a profile references them: a default location, and a list of
    calendar links (at most one ``main``, any number of ``extra``). A profile is
    edited as a v2 dict (``self._draft``) built up across the steps.
    """

    def __init__(self) -> None:
        self._profile_id: str | None = None
        # Working copy of the profile under edit (v2 shape), built up across steps.
        self._draft: dict[str, Any] = {}
        # Registry records created inline by the profile flow, saved with it.
        self._new_location: dict[str, Any] | None = None
        self._new_calendar: dict[str, Any] | None = None
        self._new_calendar_url: str | None = None
        # Links being configured: calendar ids still to visit and the results.
        self._link_queue: list[str] = []
        self._links: dict[str, dict[str, Any]] = {}
        self._main_calendar: str | None = None
        # Registry editing state.
        self._location_id: str | None = None
        self._calendar_id: str | None = None
        # Id pending removal (profile or calendar), carried to the confirm step.
        self._remove_id: str | None = None

    # -- Menu ---------------------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the top-level options menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "add_profile",
                "edit_profile",
                "remove_profile",
                "locations",
                "calendars",
                "settings",
            ],
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the global dry-run flag and optional test-recipient script."""
        options = dict(self.config_entry.options)
        if user_input is not None:
            options[OPT_DRY_RUN] = user_input[OPT_DRY_RUN]
            script = user_input.get(OPT_TEST_RECIPIENT_SCRIPT)
            if script:
                options[OPT_TEST_RECIPIENT_SCRIPT] = script
            else:
                options.pop(OPT_TEST_RECIPIENT_SCRIPT, None)
            return self.async_create_entry(data=options)

        current_script = options.get(OPT_TEST_RECIPIENT_SCRIPT)
        schema_dict: dict[Any, Any] = {
            vol.Required(
                OPT_DRY_RUN, default=options.get(OPT_DRY_RUN, DEFAULT_DRY_RUN)
            ): selector.BooleanSelector(),
        }
        script_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="script")
        )
        if current_script:
            schema_dict[
                vol.Optional(OPT_TEST_RECIPIENT_SCRIPT, default=current_script)
            ] = script_selector
        else:
            schema_dict[vol.Optional(OPT_TEST_RECIPIENT_SCRIPT)] = script_selector
        return self.async_show_form(
            step_id="settings", data_schema=vol.Schema(schema_dict)
        )

    # -- Locations registry -------------------------------------------------

    async def async_step_locations(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The locations submenu."""
        return self.async_show_menu(
            step_id="locations",
            menu_options=["location_add", "location_edit", "location_remove"],
        )

    async def async_step_location_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._location_id = None
        return await self.async_step_location_form()

    async def async_step_location_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a location to edit."""
        locations = self._locations()
        if not locations:
            return self.async_abort(reason="no_locations")
        if user_input is not None:
            self._location_id = user_input["location_id"]
            return await self.async_step_location_form()
        return self.async_show_form(
            step_id="location_edit",
            data_schema=vol.Schema(
                {
                    vol.Required("location_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=_registry_options(locations)
                        )
                    )
                }
            ),
        )

    async def async_step_location_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name, map position, aliases and optional static minutes."""
        locations = self._locations()
        existing = locations.get(self._location_id or "", {})
        if user_input is not None:
            point = user_input["location"]
            static_minutes = user_input.get("static_minutes")
            location_id = self._location_id or _generate_id(
                user_input["name"], locations, fallback="location"
            )
            locations[location_id] = {
                "id": location_id,
                "name": user_input["name"],
                "lat": point["latitude"],
                "lon": point["longitude"],
                "aliases": list(_split_patterns(user_input.get("aliases", ""))),
                "static_minutes": (
                    int(static_minutes) if static_minutes is not None else None
                ),
            }
            options = dict(self.config_entry.options)
            options["locations"] = locations
            return self.async_create_entry(data=options)

        # The frontend crashes on a location selector with no non-null suggested
        # value, so seed it with the stored point or the household home.
        point = {
            "latitude": existing.get("lat", self.config_entry.data.get(DATA_HOME_LAT)),
            "longitude": existing.get("lon", self.config_entry.data.get(DATA_HOME_LON)),
        }
        schema = vol.Schema(
            {
                vol.Required(
                    "name", default=existing.get("name", "")
                ): selector.TextSelector(),
                vol.Required(
                    "location", description={"suggested_value": point}
                ): selector.LocationSelector(),
                vol.Optional(
                    "aliases", default=_join_patterns(existing.get("aliases", ()))
                ): selector.TextSelector(),
                vol.Optional(
                    "static_minutes",
                    description={"suggested_value": existing.get("static_minutes")},
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=240, mode=selector.NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(step_id="location_form", data_schema=schema)

    async def async_step_location_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove a location, unless a profile default or a link uses it."""
        locations = self._locations()
        if not locations:
            return self.async_abort(reason="no_locations")
        errors: dict[str, str] = {}
        if user_input is not None:
            location_id = user_input["location_id"]
            if location_id in _location_ids_in_use(self._profiles()):
                errors["location_id"] = "location_in_use"
            else:
                locations.pop(location_id, None)
                options = dict(self.config_entry.options)
                options["locations"] = locations
                return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="location_remove",
            data_schema=vol.Schema(
                {
                    vol.Required("location_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=_registry_options(locations)
                        )
                    )
                }
            ),
            errors=errors,
        )

    # -- Calendars registry -------------------------------------------------

    async def async_step_calendars(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The calendars submenu."""
        return self.async_show_menu(
            step_id="calendars",
            menu_options=["calendar_add", "calendar_edit", "calendar_remove"],
        )

    async def async_step_calendar_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._calendar_id = None
        return await self.async_step_calendar_form()

    async def async_step_calendar_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a calendar to edit."""
        calendars = self._calendars()
        if not calendars:
            return self.async_abort(reason="no_calendars")
        if user_input is not None:
            self._calendar_id = user_input["calendar_id"]
            return await self.async_step_calendar_form()
        return self.async_show_form(
            step_id="calendar_edit",
            data_schema=vol.Schema(
                {
                    vol.Required("calendar_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=_registry_options(calendars)
                        )
                    )
                }
            ),
        )

    async def async_step_calendar_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name, source and the persons (profiles) this calendar is linked to."""
        calendars = self._calendars()
        profiles = self._profiles()
        existing = calendars.get(self._calendar_id or "", {})
        linked_now = [
            pid
            for pid, profile in profiles.items()
            if self._calendar_id and self._calendar_id in _calendar_ids_of(profile)
        ]
        errors: dict[str, str] = {}
        form_defaults: Mapping[str, Any] = user_input or {}

        if user_input is not None:
            calendar, url, errors = await self._calendar_from_input(
                name=user_input["name"],
                source_type=user_input["source_type"],
                ics_url=user_input.get("ics_url"),
                entity_id=user_input.get("calendar_entity_id"),
                stored_url=self._stored_calendar_url(self._calendar_id),
            )
            if not errors:
                return self._save_calendar(
                    calendar, url, set(user_input.get("persons", []))
                )

        schema = vol.Schema(
            {
                vol.Required(
                    "name",
                    default=form_defaults.get("name", existing.get("name", "")),
                ): selector.TextSelector(),
                vol.Required(
                    "source_type",
                    default=form_defaults.get(
                        "source_type", existing.get("source_type", "ics")
                    ),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(SOURCE_TYPES), translation_key="source_type"
                    )
                ),
                # The ICS URL is a secret: a password field, never pre-filled.
                # An empty value on edit keeps the stored URL.
                vol.Optional("ics_url"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    "calendar_entity_id",
                    description={
                        "suggested_value": form_defaults.get(
                            "calendar_entity_id", existing.get("calendar_entity_id")
                        )
                    },
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="calendar")
                ),
                vol.Optional(
                    "persons",
                    default=list(form_defaults.get("persons", linked_now)),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_profile_options(profiles), multiple=True
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="calendar_form", data_schema=schema, errors=errors
        )

    async def async_step_calendar_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a calendar to remove, then confirm."""
        calendars = self._calendars()
        if not calendars:
            return self.async_abort(reason="no_calendars")
        if user_input is not None:
            self._remove_id = user_input["calendar_id"]
            return await self.async_step_calendar_remove_confirm()
        return self.async_show_form(
            step_id="calendar_remove",
            data_schema=vol.Schema(
                {
                    vol.Required("calendar_id"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=_registry_options(calendars)
                        )
                    )
                }
            ),
        )

    async def async_step_calendar_remove_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Remove the calendar, its links in every profile and its secret URL."""
        assert self._remove_id is not None
        calendar_id = self._remove_id
        if user_input is None:
            return self.async_show_form(
                step_id="calendar_remove_confirm", data_schema=vol.Schema({})
            )
        options = dict(self.config_entry.options)
        calendars = self._calendars()
        calendars.pop(calendar_id, None)
        options["calendars"] = calendars
        options[OPT_PROFILES] = {
            pid: _without_calendar(profile, calendar_id)
            for pid, profile in self._profiles().items()
        }
        data = dict(self.config_entry.data)
        ics_urls = dict(data.get(DATA_ICS_URLS, {}))
        ics_urls.pop(calendar_id, None)
        data[DATA_ICS_URLS] = ics_urls
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        return self.async_create_entry(data=options)

    @callback
    def _save_calendar(
        self,
        calendar: dict[str, Any],
        url: str | None,
        persons: set[str],
    ) -> ConfigFlowResult:
        """Persist a calendar and bring the selected persons' links in line.

        A newly linked person gets an ``extra`` link with defaults; an unlinked
        one loses their link. A calendar linked by two or more persons starts
        new links in ``tagged_or_untagged`` mode so each person's tagged events
        reach only them (plan §4.5); existing links are left as they are.
        """
        calendars = self._calendars()
        calendar_id = self._calendar_id or _generate_id(
            calendar["name"], calendars, fallback="calendar"
        )
        calendars[calendar_id] = {"id": calendar_id, **calendar}
        profiles = self._profiles()
        linked_before = {
            pid
            for pid, profile in profiles.items()
            if calendar_id in _calendar_ids_of(profile)
        }
        attendee_mode = "tagged_or_untagged" if len(persons) >= 2 else "all"
        for pid, profile in list(profiles.items()):
            linked = calendar_id in _calendar_ids_of(profile)
            if pid in persons and not linked:
                links = list(profile.get("calendar_links", ()) or ())
                link_id = _unique_link_id(calendar_id, links)
                links.append(
                    _default_link(
                        link_id,
                        calendar_id,
                        "extra",
                        label=str(calendar["name"]),
                        attendee_mode=attendee_mode,
                    )
                )
                profiles[pid] = {**profile, "calendar_links": links}
            elif pid not in persons and linked:
                profiles[pid] = _without_calendar(profile, calendar_id)
        newly_linked = {
            pid for pid in persons if pid in profiles and pid not in linked_before
        }
        _share_calendar_safely(profiles, calendar_id, newly_linked)

        options = dict(self.config_entry.options)
        options["calendars"] = calendars
        options[OPT_PROFILES] = profiles
        data = dict(self.config_entry.data)
        ics_urls = dict(data.get(DATA_ICS_URLS, {}))
        if url:
            ics_urls[calendar_id] = url
        else:
            ics_urls.pop(calendar_id, None)
        data[DATA_ICS_URLS] = ics_urls
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        return self.async_create_entry(data=options)

    async def _calendar_from_input(
        self,
        *,
        name: str,
        source_type: str,
        ics_url: str | None,
        entity_id: str | None,
        stored_url: str | None,
    ) -> tuple[dict[str, Any], str | None, dict[str, str]]:
        """Validate a calendar form; return ``(record, url, errors)``.

        Shared by the calendars submenu and the profile flow's inline "new
        calendar". The ICS URL is validated by a test fetch and never echoed
        back; an empty URL keeps ``stored_url``. Error keys are the field names
        of the calendars form (``ics_url``, ``calendar_entity_id``).
        """
        errors: dict[str, str] = {}
        record: dict[str, Any] = {
            "name": name,
            "source_type": source_type,
            "calendar_entity_id": None,
        }
        url: str | None = None
        if source_type == "ha_calendar":
            if not entity_id:
                errors["calendar_entity_id"] = "calendar_required"
            record["calendar_entity_id"] = entity_id
        else:
            url = ics_url or stored_url
            if not url:
                errors["ics_url"] = "ics_required"
            else:
                error = await self._validate_ics(url)
                if error:
                    errors["ics_url"] = error
        return record, url, errors

    # -- Profile editing ----------------------------------------------------

    async def async_step_add_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Begin adding a brand-new profile; its id is derived from the name."""
        self._reset_profile_state()
        self._draft = {"calendar_links": []}
        return await self.async_step_profile_person()

    async def async_step_edit_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick an existing profile to edit; the id stays unchanged on save."""
        profiles = self._profiles()
        if not profiles:
            return self.async_abort(reason="no_profiles")
        if user_input is not None:
            self._reset_profile_state()
            self._profile_id = user_input["profile_id"]
            self._draft = copy.deepcopy(profiles.get(self._profile_id) or {})
            return await self.async_step_profile_person()

        schema = vol.Schema(
            {
                vol.Required("profile_id"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=_profile_options(profiles))
                )
            }
        )
        return self.async_show_form(step_id="edit_profile", data_schema=schema)

    def _reset_profile_state(self) -> None:
        self._profile_id = None
        self._draft = {}
        self._new_location = None
        self._new_calendar = None
        self._new_calendar_url = None
        self._link_queue = []
        self._links = {}
        self._main_calendar = None

    async def async_step_remove_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick an existing profile to remove, then confirm."""
        profiles = self._profiles()
        if not profiles:
            return self.async_abort(reason="no_profiles")
        if user_input is not None:
            self._remove_id = user_input["profile_id"]
            return await self.async_step_remove_confirm()

        schema = vol.Schema(
            {
                vol.Required("profile_id"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=_profile_options(profiles))
                )
            }
        )
        return self.async_show_form(step_id="remove_profile", data_schema=schema)

    async def async_step_remove_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm removal: drop the profile, its device and copies to it.

        Calendars and locations are household resources. Those only this profile
        used would be left behind (with their secret URLs), so the confirm step
        lists them with a "remove these too" box that defaults on.
        """
        assert self._remove_id is not None
        remove_id = self._remove_id
        profiles = dict(self._profiles())
        removed = profiles.pop(remove_id, None) or {}
        calendars = self._calendars()
        locations = self._locations()
        unused_calendars = sorted(
            (_calendar_ids_of(removed) - _calendar_ids_in_use(profiles))
            & set(calendars)
        )
        unused_locations = sorted(
            (_location_ids_of(removed) - _location_ids_in_use(profiles))
            & set(locations)
        )
        has_unused = bool(unused_calendars or unused_locations)

        if user_input is not None:
            # Nobody keeps copying notices to a profile that no longer exists.
            for pid, profile in list(profiles.items()):
                if remove_id in (profile.get("copy_to") or ()):
                    profiles[pid] = {
                        **profile,
                        "copy_to": [
                            other for other in profile["copy_to"] if other != remove_id
                        ],
                    }
            options = dict(self.config_entry.options)
            options[OPT_PROFILES] = profiles

            if has_unused and user_input.get("remove_unused", True):
                data = dict(self.config_entry.data)
                ics_urls = dict(data.get(DATA_ICS_URLS, {}))
                for calendar_id in unused_calendars:
                    calendars.pop(calendar_id, None)
                    ics_urls.pop(calendar_id, None)
                for location_id in unused_locations:
                    locations.pop(location_id, None)
                data[DATA_ICS_URLS] = ics_urls
                options["calendars"] = calendars
                options["locations"] = locations
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=data
                )

            # Purge the profile's device; this cascades its entities.
            device_registry = dr.async_get(self.hass)
            device = device_registry.async_get_device(
                identifiers={(DOMAIN, f"{self.config_entry.entry_id}_{remove_id}")}
            )
            if device is not None:
                device_registry.async_remove_device(device.id)

            return self.async_create_entry(data=options)

        names = [str(calendars[cid].get("name", cid)) for cid in unused_calendars] + [
            str(locations[lid].get("name", lid)) for lid in unused_locations
        ]
        schema = (
            vol.Schema(
                {
                    vol.Required(
                        "remove_unused", default=True
                    ): selector.BooleanSelector()
                }
            )
            if has_unused
            else vol.Schema({})
        )
        return self.async_show_form(
            step_id="remove_confirm",
            data_schema=schema,
            description_placeholders={"unused": ", ".join(names) or "-"},
        )

    async def async_step_profile_person(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The person (HA ``person`` entity), display name and adult flag.

        The person is required for a new profile (it matches attendee tags in
        shared calendars) and may be bound to one profile only.
        """
        errors: dict[str, str] = {}
        is_new = self._profile_id is None
        if user_input is not None:
            # An omitted person or name keeps the profile's own (edit).
            person = (
                user_input.get("person_entity_id")
                or self._draft.get("person_entity_id")
                or None
            )
            name = (
                (user_input.get("name") or "").strip()
                or self._draft.get("name")
                or _person_name(self.hass, person)
            )
            if person is not None and any(
                other_id != self._profile_id and other.get("person_entity_id") == person
                for other_id, other in self._profiles().items()
            ):
                errors["person_entity_id"] = "person_in_use"
            elif not name:
                errors["name"] = "name_required"
            if not errors:
                self._draft["person_entity_id"] = person
                self._draft["name"] = name
                self._draft["is_adult"] = user_input["is_adult"]
                return await self.async_step_profile_destination()

        person_key: Any = (
            vol.Required("person_entity_id")
            if is_new
            else vol.Optional(
                "person_entity_id",
                description={"suggested_value": self._draft.get("person_entity_id")},
            )
        )
        schema = vol.Schema(
            {
                person_key: selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="person")
                ),
                vol.Optional(
                    "name", description={"suggested_value": self._draft.get("name")}
                ): selector.TextSelector(),
                vol.Required(
                    "is_adult", default=self._draft.get("is_adult", False)
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="profile_person", data_schema=schema, errors=errors
        )

    async def async_step_profile_destination(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Default location (registry or inline new), mode and mode settings.

        SL settings (walk/transfer margins) stay available regardless of the
        chosen default mode so a car-default profile still has a
        working transit plan (spec §4.3).
        """
        errors: dict[str, str] = {}
        locations = self._locations()
        if user_input is not None:
            new_name = (user_input.get("new_location_name") or "").strip()
            chosen = user_input.get("default_location")
            if new_name:
                point = user_input["destination_location"]
                new_id = _generate_id(new_name, locations, fallback="location")
                self._new_location = {
                    "id": new_id,
                    "name": new_name,
                    "lat": point["latitude"],
                    "lon": point["longitude"],
                    "aliases": [],
                    "static_minutes": None,
                }
                self._draft["default_location_id"] = new_id
            elif chosen and chosen in locations:
                self._new_location = None
                self._draft["default_location_id"] = chosen
            elif not chosen and self._draft.get("default_location_id") in locations:
                self._new_location = None  # an omitted choice keeps the saved one
            else:
                errors["default_location"] = "location_required"
            if not errors:
                self._draft["default_mode"] = user_input["default_mode"]
                self._draft["static_minutes"] = user_input.get("static_minutes")
                self._draft["static_label"] = user_input.get("static_label") or None
                self._draft["weather_adjust"] = user_input["weather_adjust"]
                self._draft["car_fallback_minutes"] = user_input.get(
                    "car_fallback_minutes"
                )
                return await self.async_step_profile_calendars()

        current = locations.get(self._draft.get("default_location_id", ""))
        # A `None` suggested value fails the frontend's non-null check and
        # crashes the location selector, so fall back to the household home.
        dest_default = {
            "latitude": (current or {}).get(
                "lat", self.config_entry.data.get(DATA_HOME_LAT)
            ),
            "longitude": (current or {}).get(
                "lon", self.config_entry.data.get(DATA_HOME_LON)
            ),
        }
        location_key: Any = (
            vol.Optional(
                "default_location",
                description={"suggested_value": self._draft.get("default_location_id")},
            )
            if locations
            else vol.Optional("default_location")
        )
        schema = vol.Schema(
            {
                location_key: selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_registry_options(locations),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional("new_location_name"): selector.TextSelector(),
                vol.Optional(
                    "destination_location",
                    description={"suggested_value": dest_default},
                ): selector.LocationSelector(),
                vol.Required(
                    "default_mode",
                    default=self._draft.get("default_mode", "public_transport"),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(MODES), translation_key="mode"
                    )
                ),
                vol.Optional(
                    "static_minutes",
                    description={"suggested_value": self._draft.get("static_minutes")},
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=240, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    "static_label",
                    description={"suggested_value": self._draft.get("static_label")},
                ): selector.TextSelector(),
                vol.Required(
                    "weather_adjust",
                    default=self._draft.get("weather_adjust", False),
                ): selector.BooleanSelector(),
                vol.Optional(
                    "car_fallback_minutes",
                    description={
                        "suggested_value": self._draft.get("car_fallback_minutes")
                    },
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=240, mode=selector.NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="profile_destination", data_schema=schema, errors=errors
        )

    async def async_step_profile_calendars(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Which calendars the profile links, which is the main one, or a new one.

        Zero links are allowed (a profile that only receives copies). An inline
        "new calendar" is validated like the calendars form and saved with the
        profile.
        """
        errors: dict[str, str] = {}
        calendars = self._calendars()
        stored_links = {
            link["calendar_id"]: link
            for link in self._draft.get("calendar_links", ()) or ()
        }
        stored_main = next(
            (cid for cid, link in stored_links.items() if link.get("role") == "main"),
            None,
        )
        if user_input is not None:
            selected = [c for c in user_input.get("calendars", []) if c in calendars]
            main = user_input.get("main_calendar") or None
            new_name = (user_input.get("new_calendar_name") or "").strip()
            self._new_calendar = None
            self._new_calendar_url = None
            new_id: str | None = None
            if new_name:
                record, url, errors = await self._calendar_from_input(
                    name=new_name,
                    source_type=user_input.get("new_calendar_source_type", "ics"),
                    ics_url=user_input.get("new_calendar_ics_url"),
                    entity_id=user_input.get("new_calendar_entity_id"),
                    stored_url=None,
                )
                errors = {
                    {
                        "ics_url": "new_calendar_ics_url",
                        "calendar_entity_id": "new_calendar_entity_id",
                    }.get(field, field): code
                    for field, code in errors.items()
                }
                duplicate = _calendar_with_same_source(
                    record, url, calendars, self.config_entry.data
                )
                if not errors and duplicate in selected:
                    errors["new_calendar_name"] = "duplicate_link"
                if not errors:
                    if duplicate is not None:
                        # The same source already exists: link it, do not copy it.
                        new_id = duplicate
                    else:
                        new_id = _generate_id(new_name, calendars, fallback="calendar")
                        self._new_calendar = {"id": new_id, **record}
                        self._new_calendar_url = url
                    if new_id not in selected:
                        selected.append(new_id)
            if main == NEW_CALENDAR:
                main = new_id
                if main is None and not errors:
                    errors["main_calendar"] = "main_not_linked"
            if not errors and main is not None and main not in selected:
                errors["main_calendar"] = "main_not_linked"
            if not errors:
                self._main_calendar = main
                self._link_queue = list(selected)
                self._links = {}
                return await self.async_step_profile_link()

        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Optional(
                    "calendars",
                    default=list(defaults.get("calendars", stored_links)),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_registry_options(calendars), multiple=True
                    )
                ),
                vol.Optional(
                    "main_calendar",
                    description={
                        "suggested_value": defaults.get("main_calendar", stored_main)
                    },
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            *_registry_options(calendars),
                            selector.SelectOptionDict(
                                value=NEW_CALENDAR,
                                label=_flow_label(self.hass, "new_calendar"),
                            ),
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional("new_calendar_name"): selector.TextSelector(),
                vol.Optional(
                    "new_calendar_source_type",
                    default=defaults.get("new_calendar_source_type", "ics"),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(SOURCE_TYPES), translation_key="source_type"
                    )
                ),
                vol.Optional("new_calendar_ics_url"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional("new_calendar_entity_id"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="calendar")
                ),
            }
        )
        return self.async_show_form(
            step_id="profile_calendars", data_schema=schema, errors=errors
        )

    def _reserved_links(self) -> list[Mapping[str, Any]]:
        """Links whose ids a new link must avoid: the ones already built plus the
        stored ids that links still in the queue are going to keep."""
        queued = set(self._link_queue)
        kept = [
            link
            for link in self._draft.get("calendar_links", ()) or ()
            if link.get("calendar_id") in queued and link.get("link_id")
        ]
        return [*self._links.values(), *kept]

    async def async_step_profile_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Settings of one calendar link; repeated for every linked calendar."""
        if not self._link_queue:
            return await self.async_step_profile_margins()
        calendar_id = self._link_queue[0]
        registry = {**self._calendars(), **self._pending_calendars()}
        locations = {**self._locations(), **self._pending_locations()}
        stored: Mapping[str, Any] = next(
            (
                link
                for link in self._draft.get("calendar_links", ()) or ()
                if link.get("calendar_id") == calendar_id
            ),
            {},
        )
        errors: dict[str, str] = {}
        role = "main" if calendar_id == self._main_calendar else "extra"

        if user_input is not None:
            try:
                rules = parse_rules_text(
                    user_input.get("rules", ""), _locations_by_id(locations)
                )
            except RulesError:
                errors["rules"] = "rules_invalid"
            if not errors:
                location = user_input.get("location") or None
                mode = user_input.get("mode") or None
                static_minutes = user_input.get("static_minutes")
                arrival_margin = user_input.get("arrival_margin")
                self._links[calendar_id] = {
                    "link_id": stored.get("link_id")
                    or _unique_link_id(calendar_id, self._reserved_links()),
                    "calendar_id": calendar_id,
                    "role": role,
                    "label": user_input.get("label", "") or "",
                    "include_patterns": list(
                        _split_patterns(user_input.get("include_patterns", ""))
                    ),
                    "exclude_patterns": list(
                        _split_patterns(user_input.get("exclude_patterns", ""))
                    ),
                    "attendee_mode": user_input["attendee_mode"],
                    "location_id": None if location == LINK_DEFAULT else location,
                    "mode": None if mode == LINK_DEFAULT else mode,
                    "static_minutes": (
                        int(static_minutes) if static_minutes is not None else None
                    ),
                    "arrival_margin": (
                        int(arrival_margin) if arrival_margin is not None else None
                    ),
                    "rules": [
                        {"match": r.match, "location_id": r.location_id, "mode": r.mode}
                        for r in rules
                    ],
                    "match_event_location": user_input["match_event_location"],
                    "show_summary": user_input["show_summary"],
                }
                self._link_queue.pop(0)
                return await self.async_step_profile_link()

        defaults = user_input or {}
        linkers = sum(
            1
            for pid, profile in self._profiles().items()
            if pid != self._profile_id and calendar_id in _calendar_ids_of(profile)
        )
        default_attendee = (
            "tagged_or_untagged" if role == "extra" and linkers >= 1 else "all"
        )
        # A link demoted from main keeps no stored mode: it now shares the
        # calendar as an extra link, like a newly joined one.
        stored_attendee = (
            None
            if role == "extra" and stored.get("role") == "main" and linkers >= 1
            else stored.get("attendee_mode")
        )
        rules_text = defaults.get("rules")
        if rules_text is None:
            rules_text = format_rules_text(
                stored.get("rules", ()) or (), _locations_by_id(locations)
            )
        schema = vol.Schema(
            {
                vol.Optional(
                    "label",
                    default=defaults.get(
                        "label",
                        stored.get(
                            "label", registry.get(calendar_id, {}).get("name", "")
                        ),
                    ),
                ): selector.TextSelector(),
                vol.Optional(
                    "include_patterns",
                    default=defaults.get(
                        "include_patterns",
                        _join_patterns(stored.get("include_patterns", ())),
                    ),
                ): selector.TextSelector(),
                vol.Optional(
                    "exclude_patterns",
                    default=defaults.get(
                        "exclude_patterns",
                        _join_patterns(stored.get("exclude_patterns", ())),
                    ),
                ): selector.TextSelector(),
                vol.Required(
                    "attendee_mode",
                    default=defaults.get(
                        "attendee_mode", stored_attendee or default_attendee
                    ),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(ATTENDEE_MODES), translation_key="attendee_mode"
                    )
                ),
                vol.Optional(
                    "location",
                    default=defaults.get(
                        "location", stored.get("location_id") or LINK_DEFAULT
                    ),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=LINK_DEFAULT,
                                label=_flow_label(self.hass, "profile_default"),
                            ),
                            *_registry_options(locations),
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    "mode",
                    default=defaults.get("mode", stored.get("mode") or LINK_DEFAULT),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[LINK_DEFAULT, *MODES],
                        translation_key="mode_or_default",
                    )
                ),
                vol.Optional(
                    "static_minutes",
                    description={"suggested_value": stored.get("static_minutes")},
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=240, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    "arrival_margin",
                    description={"suggested_value": stored.get("arrival_margin")},
                ): _minutes_selector(),
                vol.Optional("rules", default=rules_text): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True)
                ),
                vol.Required(
                    "match_event_location",
                    default=defaults.get(
                        "match_event_location",
                        stored.get("match_event_location", False),
                    ),
                ): selector.BooleanSelector(),
                vol.Required(
                    "show_summary",
                    default=defaults.get(
                        "show_summary", stored.get("show_summary", False)
                    ),
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="profile_link",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "calendar": str(registry.get(calendar_id, {}).get("name", calendar_id)),
                "role": role,
            },
        )

    async def async_step_profile_margins(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Margins (minutes), the adult flag and the expected-weekday mask.

        ``is_adult`` is an explicit per-profile boolean: an adult's calendar
        start already is the arrival requirement, so their default arrival
        margin is 0, while a child gets a few minutes to reach the classroom
        (spec §4.3).
        """
        if user_input is not None:
            self._draft["is_adult"] = user_input["is_adult"]
            self._draft["arrival"] = int(user_input["arrival"])
            self._draft["departure"] = int(user_input["departure"])
            self._draft["boarding"] = int(user_input["boarding"])
            self._draft["parking_and_walk"] = int(user_input["parking_and_walk"])
            self._draft["min_transfer"] = int(user_input["min_transfer"])
            self._draft["weekday_mask"] = tuple(user_input["weekday_mask"])
            return await self.async_step_profile_packing()

        is_adult = self._draft.get("is_adult", False)
        arrival_default = (
            DEFAULT_ARRIVAL_MARGIN_ADULT if is_adult else DEFAULT_ARRIVAL_MARGIN_CHILD
        )
        schema = vol.Schema(
            {
                vol.Required("is_adult", default=is_adult): selector.BooleanSelector(),
                vol.Required(
                    "arrival", default=self._draft.get("arrival", arrival_default)
                ): _minutes_selector(),
                vol.Required(
                    "departure",
                    default=self._draft.get("departure", DEFAULT_DEPARTURE_MARGIN),
                ): _minutes_selector(),
                vol.Required(
                    "boarding",
                    default=self._draft.get("boarding", DEFAULT_BOARDING_MARGIN),
                ): _minutes_selector(),
                vol.Required(
                    "parking_and_walk",
                    default=self._draft.get(
                        "parking_and_walk", DEFAULT_PARKING_WALK_MARGIN
                    ),
                ): _minutes_selector(),
                vol.Required(
                    "min_transfer",
                    default=self._draft.get(
                        "min_transfer", DEFAULT_MIN_TRANSFER_MARGIN
                    ),
                ): _minutes_selector(),
                vol.Required(
                    "weekday_mask",
                    default=list(self._draft.get("weekday_mask", DEFAULT_WEEKDAY_MASK)),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=day, label=WEEKDAY_LABELS[day]
                            )
                            for day in WEEKDAY_OPTIONS
                        ],
                        multiple=True,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="profile_margins", data_schema=schema)

    async def async_step_profile_packing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """One optional packing rule, with a preview of upcoming matching days."""
        if user_input is not None:
            rules = []
            match = (user_input.get("packing_match") or "").strip()
            item = (user_input.get("packing_item") or "").strip()
            if match and item:
                rules.append({"id": "rule_1", "match": match, "item": item})
            self._draft["packing_rules"] = rules
            return await self.async_step_profile_notifications()

        preview = await self._packing_preview()
        schema = vol.Schema(
            {
                vol.Optional(
                    "packing_match",
                    default=_first_rule_field(self._draft, "match"),
                ): selector.TextSelector(),
                vol.Optional(
                    "packing_item",
                    default=_first_rule_field(self._draft, "item"),
                ): selector.TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="profile_packing",
            data_schema=schema,
            description_placeholders={"preview": preview},
        )

    async def async_step_profile_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Quiet hours, channel scripts, copies, lunch and notification toggles."""
        if user_input is not None:
            self._draft["quiet_start"] = user_input["quiet_start"]
            self._draft["quiet_end"] = user_input["quiet_end"]
            self._draft["notifications_enabled"] = user_input["notifications_enabled"]
            self._draft["evening_notice_enabled"] = user_input["evening_notice_enabled"]
            self._draft["change_threshold_minutes"] = int(
                user_input["change_threshold_minutes"]
            )
            scripts: dict[str, str] = {}
            for channel in NOTIFICATION_CHANNELS:
                value = user_input.get(f"script_{channel}")
                if value:
                    scripts[channel] = value
            self._draft["scripts"] = scripts
            self._draft["copy_to"] = list(user_input.get("copy_to", []))
            self._draft["copy_kinds"] = sorted(
                user_input.get("copy_kinds", ALL_COPY_KINDS)
            )
            self._draft["lunch_calendar_entity_id"] = (
                user_input.get("lunch_calendar_entity_id") or None
            )
            self._draft["lunch_in_digest"] = user_input["lunch_in_digest"]
            return await self.async_step_profile_test()

        existing_scripts = self._draft.get("scripts", {})
        script_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="script")
        )
        others = {
            pid: profile
            for pid, profile in self._profiles().items()
            if pid != self._profile_id
        }
        schema_dict: dict[Any, Any] = {
            vol.Required(
                "quiet_start", default=self._draft.get("quiet_start", "22:00:00")
            ): selector.TimeSelector(),
            vol.Required(
                "quiet_end", default=self._draft.get("quiet_end", "06:00:00")
            ): selector.TimeSelector(),
            vol.Required(
                "notifications_enabled",
                default=self._draft.get("notifications_enabled", False),
            ): selector.BooleanSelector(),
            vol.Required(
                "evening_notice_enabled",
                default=self._draft.get("evening_notice_enabled", True),
            ): selector.BooleanSelector(),
            vol.Required(
                "change_threshold_minutes",
                default=self._draft.get(
                    "change_threshold_minutes", DEFAULT_CHANGE_THRESHOLD_MINUTES
                ),
            ): _minutes_selector(),
            vol.Optional(
                "copy_to",
                default=[c for c in self._draft.get("copy_to", ()) if c in others],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_profile_options(others), multiple=True
                )
            ),
            vol.Optional(
                "copy_kinds",
                default=list(self._draft.get("copy_kinds", sorted(ALL_COPY_KINDS))),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=sorted(ALL_COPY_KINDS),
                    multiple=True,
                    translation_key="copy_kind",
                )
            ),
            vol.Optional(
                "lunch_calendar_entity_id",
                description={
                    "suggested_value": self._draft.get("lunch_calendar_entity_id")
                },
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="calendar")
            ),
            vol.Required(
                "lunch_in_digest", default=self._draft.get("lunch_in_digest", True)
            ): selector.BooleanSelector(),
        }
        # Channel scripts are optional per profile so a child profile can be
        # configured before that child has a phone (spec Environment facts, §4.1).
        for channel in NOTIFICATION_CHANNELS:
            key = f"script_{channel}"
            if existing_scripts.get(channel):
                schema_dict[vol.Optional(key, default=existing_scripts[channel])] = (
                    script_selector
                )
            else:
                schema_dict[vol.Optional(key)] = script_selector
        return self.async_show_form(
            step_id="profile_notifications", data_schema=vol.Schema(schema_dict)
        )

    async def async_step_profile_test(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Read-only test calculation, then persist the profile.

        The flow performs no network calls here (that is the coordinator's job,
        T13). It computes a representative plan for both car and transit using a
        fixed sample travel time, so a car-default profile still
        shows a transit plan (spec §4.1 pt 7, §4.3), plus one sample line per
        calendar link.
        """
        if user_input is not None:
            return self._save_profile()

        summary = self._test_calculation()
        return self.async_show_form(
            step_id="profile_test",
            data_schema=vol.Schema({}),
            description_placeholders={"summary": summary},
        )

    # -- Persistence --------------------------------------------------------

    @callback
    def _save_profile(self) -> ConfigFlowResult:
        """Write the draft profile (and any inline new records) and finish.

        On add (``self._profile_id`` is None) the id is a stable slug derived
        from the name; on edit the existing id is kept unchanged. Locations and
        calendars are registry resources: the profile only references them, and
        an inline new one is saved alongside it. A new calendar's secret URL
        goes to ``entry.data``, never to options.
        """
        profiles = self._profiles()
        if self._profile_id is None:
            self._profile_id = _generate_profile_id(
                self._draft["name"], profiles.keys()
            )
        profile_id = self._profile_id
        links = [self._links[cid] for cid in self._links]
        profile = {
            **self._draft,
            "id": profile_id,
            "calendar_links": links,
        }
        profile.setdefault("copy_to", [])
        profile.setdefault("copy_kinds", sorted(ALL_COPY_KINDS))
        profile.setdefault("lunch_calendar_entity_id", None)
        profile.setdefault("lunch_in_digest", True)
        previous = _calendar_ids_of(profiles.get(profile_id) or {})
        profiles[profile_id] = profile
        for calendar_id in _calendar_ids_of(profile) - previous:
            _share_calendar_safely(profiles, calendar_id, {profile_id})

        options = dict(self.config_entry.options)
        options[OPT_PROFILES] = profiles
        if self._new_location is not None:
            locations = self._locations()
            locations[self._new_location["id"]] = self._new_location
            options["locations"] = locations
        if self._new_calendar is not None:
            calendars = self._calendars()
            calendars[self._new_calendar["id"]] = self._new_calendar
            options["calendars"] = calendars
            if self._new_calendar_url:
                data = dict(self.config_entry.data)
                ics_urls = dict(data.get(DATA_ICS_URLS, {}))
                ics_urls[self._new_calendar["id"]] = self._new_calendar_url
                data[DATA_ICS_URLS] = ics_urls
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=data
                )
        return self.async_create_entry(data=options)

    # -- Helpers ------------------------------------------------------------

    @callback
    def _profiles(self) -> dict[str, Any]:
        return dict(self.config_entry.options.get(OPT_PROFILES, {}))

    @callback
    def _locations(self) -> dict[str, Any]:
        return dict(self.config_entry.options.get("locations", {}))

    @callback
    def _calendars(self) -> dict[str, Any]:
        return dict(self.config_entry.options.get("calendars", {}))

    @callback
    def _pending_locations(self) -> dict[str, Any]:
        """The inline new location of the profile being edited, if any."""
        if self._new_location is None:
            return {}
        return {self._new_location["id"]: self._new_location}

    @callback
    def _pending_calendars(self) -> dict[str, Any]:
        """The inline new calendar of the profile being edited, if any."""
        if self._new_calendar is None:
            return {}
        return {self._new_calendar["id"]: self._new_calendar}

    @callback
    def _stored_calendar_url(self, calendar_id: str | None) -> str | None:
        """A calendar's stored ICS URL, so an empty URL field keeps it."""
        if calendar_id is None:
            return None
        url = self.config_entry.data.get(DATA_ICS_URLS, {}).get(calendar_id)
        return str(url) if url else None

    async def _validate_ics(self, url: str) -> str | None:
        """Fetch and parse the ICS feed once; return an error code or None.

        The URL (and therefore its token) is never echoed back: only a generic
        error code is returned to the form (review-privacy-security).
        """
        session = async_get_clientsession(self.hass)
        provider = IcsScheduleProvider(session, url, SourceFilter(), "validate")
        try:
            raw, _changed, _modified = await provider.async_fetch()
        except IcsFetchError:
            return "cannot_connect"
        except aiohttp.ClientError:
            return "cannot_connect"
        try:
            await self.hass.async_add_executor_job(
                parse_ics, raw, date.today(), SourceFilter(), "validate"
            )
        except Exception as err:  # noqa: BLE001 - any parse failure is invalid
            _LOGGER.debug("ICS validation parse failed: %s", type(err).__name__)
            return "invalid_calendar"
        return None

    def _preview_providers(self) -> list[tuple[Any, CalendarLink]]:
        """Schedule providers for the draft's links, with their link filters."""
        registry = {**self._calendars(), **self._pending_calendars()}
        urls = dict(self.config_entry.data.get(DATA_ICS_URLS, {}))
        if self._new_calendar is not None and self._new_calendar_url:
            urls[self._new_calendar["id"]] = self._new_calendar_url
        providers: list[tuple[Any, CalendarLink]] = []
        for raw_link in self._links.values():
            link = _link_from_raw(raw_link)
            calendar = registry.get(link.calendar_id)
            if calendar is None:
                continue
            provider: Any = None
            if calendar.get("source_type") == "ics" and urls.get(link.calendar_id):
                provider = IcsScheduleProvider(
                    async_get_clientsession(self.hass),
                    urls[link.calendar_id],
                    SourceFilter(),
                    "preview",
                )
            elif calendar.get("source_type") == "ha_calendar" and calendar.get(
                "calendar_entity_id"
            ):
                from .providers.ha_calendar import HaCalendarScheduleProvider

                provider = HaCalendarScheduleProvider(
                    self.hass,
                    calendar["calendar_entity_id"],
                    SourceFilter(),
                    "preview",
                )
            if provider is not None:
                providers.append((provider, link))
        return providers

    async def _packing_preview(self) -> str:
        """Describe which upcoming days a packing rule would match (§4.1 pt 4)."""
        rules = self._draft.get("packing_rules", [])
        match = ""
        if rules:
            match = rules[0].get("match", "")
        if not match:
            return "Ingen regel angiven."

        providers = self._preview_providers()
        if not providers:
            return "Länka en kalender för förhandsvisning."

        folded = match.casefold()
        today = datetime.now(UTC).astimezone(TZ).date()
        hits: list[str] = []
        for offset in range(PACKING_PREVIEW_DAYS):
            day = today + timedelta(days=offset)
            matched = False
            for provider, link in providers:
                try:
                    result = await provider.async_get_day(day)
                except Exception:  # noqa: BLE001 - preview must never break the flow
                    return "Kunde inte förhandsvisa (kontrollera källan)."
                events = filter_for_link(result.events, link)
                if any(folded in event.summary.casefold() for event in events):
                    matched = True
                    break
            if matched:
                hits.append(day.isoformat())
                if len(hits) >= PACKING_PREVIEW_LIMIT:
                    break
        if not hits:
            return (
                f"Inga träffar på '{match}' de närmaste {PACKING_PREVIEW_DAYS} dagarna."
            )
        return f"'{match}' matchar: {', '.join(hits)}"

    @callback
    def _test_calculation(self) -> str:
        """Build a read-only plan summary for both car and transit (§4.3).

        When adding a new profile the id is not assigned yet (it is derived
        from the name on save), so a provisional slug is used purely for this
        read-only preview; the persisted id is generated in ``_save_profile``.
        Ends with one sample line per calendar link.
        """
        preview_id = self._profile_id or (
            _generate_profile_id(self._draft.get("name", ""), self._profiles().keys())
        )
        try:
            profile = _profile_from_options(
                preview_id,
                {**self._draft, "calendar_links": list(self._links.values())},
            )
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.debug("Test calculation skipped: %s", type(err).__name__)
            return "Testberäkning ej tillgänglig (ofullständig profil)."

        locations = {**self._locations(), **self._pending_locations()}
        registry = {**self._calendars(), **self._pending_calendars()}

        def location_name(location_id: str) -> str:
            return str(locations.get(location_id, {}).get("name", location_id))

        # Deadline from tomorrow 08:00 local minus arrival margin, so the result
        # is representative without reading any live schedule.
        tomorrow = datetime.now(UTC).astimezone(TZ).date() + timedelta(days=1)
        event_start = combine_local(tomorrow, time(8, 0))
        deadline = event_start - timedelta(minutes=profile.margins.arrival)
        req = ArrivalRequirement(
            mission_id=make_mission_id(profile.id, tomorrow),
            person_id=profile.id,
            local_date=tomorrow,
            event_id="preview",
            event_start=event_start,
            arrival_deadline=deadline,
            destination_id=profile.default_location_id,
            source="preview",
        )
        now = datetime.now(UTC)
        lines = [
            f"Destination: {location_name(profile.default_location_id)}",
            f"Standardfärdsätt: {profile.default_mode}",
            f"Exempelstart imorgon: {_fmt(event_start)}",
        ]
        # Always show car and transit (transit uses the same fixed sample time
        # here; the real journey search runs in the coordinator, T13).
        duration = DurationResult(
            minutes=_PREVIEW_TRAVEL_MINUTES,
            fetched_at=now,
            source="static",
            quality="estimated",
        )
        preview_modes: tuple[tuple[Mode, str], ...] = (
            ("car", "Bil"),
            ("static", "Kollektivt (uppskattat)"),
        )
        for mode, label in preview_modes:
            plan = plan_fixed(req, mode, duration, profile, None, now, 0)
            lines.append(
                f"{label}: rekommenderad avgång {_fmt(plan.recommended_leave)}, "
                f"senast {_fmt(plan.latest_leave)}"
            )
        for link in profile.calendar_links:
            calendar_name = registry.get(link.calendar_id, {}).get(
                "name", link.calendar_id
            )
            destination = location_name(link.location_id or profile.default_location_id)
            kind = "huvud" if link.role == "main" else "extra"
            lines.append(
                f"Länk {link.label or calendar_name} ({kind}): "
                f"{len(link.rules)} regler, till {destination}"
            )
        if not profile.calendar_links:
            lines.append("Inga kalendrar länkade.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _generate_id(name: str, existing: Iterable[str], *, fallback: str) -> str:
    """A slug of ``name`` that is not in ``existing`` (numeric suffix on clash)."""
    taken = set(existing)
    base = slugify(name) or fallback
    if base not in taken:
        return base
    suffix = 2
    while f"{base}_{suffix}" in taken:
        suffix += 1
    return f"{base}_{suffix}"


def _generate_profile_id(name: str, existing: Iterable[str]) -> str:
    """Derive a stable slug id from a profile name, unique within ``existing``.

    The id is generated once at creation and stays stable across renames. On a
    collision a numeric suffix (``_2``, ``_3``, ...) is appended.
    """
    return _generate_id(name, existing, fallback="profile")


def _profile_options(
    profiles: Mapping[str, Any],
) -> list[selector.SelectOptionDict]:
    """Build SelectSelector options over configured profiles (value=id)."""
    return [
        selector.SelectOptionDict(
            value=pid,
            label=str(profile.get("name", pid)),
        )
        for pid, profile in profiles.items()
    ]


def _registry_options(
    records: Mapping[str, Any],
) -> list[selector.SelectOptionDict]:
    """SelectSelector options over a locations/calendars registry."""
    return [
        selector.SelectOptionDict(value=rid, label=str(record.get("name", rid)))
        for rid, record in records.items()
    ]


def _minutes_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=120, mode=selector.NumberSelectorMode.BOX
        )
    )


_FLOW_LABELS = {
    "profile_default": {"sv": "(profilens standard)", "en": "(profile default)"},
    "new_calendar": {"sv": "(ny kalender)", "en": "(new calendar)"},
}


def _flow_label(hass: Any, key: str) -> str:
    """A fixed select label in the user's language (Swedish or English).

    Dynamic registry options cannot use a translation key, so the two sentinel
    entries added to them are labelled here instead.
    """
    labels = _FLOW_LABELS[key]
    return labels["sv"] if str(hass.config.language).startswith("sv") else labels["en"]


def _person_name(hass: Any, entity_id: str | None) -> str:
    """The person's friendly name (else its object id) as a default profile name."""
    if not entity_id:
        return ""
    state = hass.states.get(entity_id)
    friendly = state.attributes.get("friendly_name") if state is not None else None
    return str(friendly or entity_id.split(".", 1)[-1].replace("_", " ").title())


def _locations_by_id(records: Mapping[str, Any]) -> dict[str, Location]:
    """Registry location dicts as :class:`Location` records (for rule parsing)."""
    return {rid: _location_from_raw({"id": rid, **raw}) for rid, raw in records.items()}


def _default_link(
    link_id: str,
    calendar_id: str,
    role: str,
    *,
    label: str = "",
    attendee_mode: str = "all",
) -> dict[str, Any]:
    """A stored calendar link with no overrides."""
    return {
        "link_id": link_id,
        "calendar_id": calendar_id,
        "role": role,
        "label": label,
        "include_patterns": [],
        "exclude_patterns": [],
        "attendee_mode": attendee_mode,
        "location_id": None,
        "mode": None,
        "static_minutes": None,
        "arrival_margin": None,
        "rules": [],
        "match_event_location": False,
        "show_summary": False,
    }


def _unique_link_id(calendar_id: str, links: Iterable[Mapping[str, Any]]) -> str:
    """A link id from the calendar id, unique among ``links`` (charset-safe)."""
    taken = {str(link.get("link_id")) for link in links}
    base = slugify(calendar_id) or "link"
    link_id = base
    suffix = 2
    while link_id in taken:
        link_id = f"{base}_{suffix}"
        suffix += 1
    return link_id


def _share_calendar_safely(
    profiles: dict[str, Any], calendar_id: str, joined: Iterable[str]
) -> None:
    """After ``joined`` started linking a calendar others already had.

    The other profiles' *extra* links still on ``all`` would keep matching events
    tagged for the newcomer, so they move to ``tagged_or_untagged`` (plan §4.5):
    untagged events stay shared, tagged ones reach only the person named. A main
    link is left alone. Does nothing unless two or more profiles link it.
    """
    joined = set(joined)
    users = [
        pid
        for pid, profile in profiles.items()
        if calendar_id in _calendar_ids_of(profile)
    ]
    if not joined or len(users) < 2:
        return
    for pid in users:
        if pid in joined:
            continue
        profile = profiles[pid]
        profiles[pid] = {
            **profile,
            "calendar_links": [
                {**link, "attendee_mode": "tagged_or_untagged"}
                if link.get("calendar_id") == calendar_id
                and link.get("role") == "extra"
                and link.get("attendee_mode", "all") == "all"
                else link
                for link in profile.get("calendar_links", ())
            ],
        }


def _without_calendar(profile: Mapping[str, Any], calendar_id: str) -> dict[str, Any]:
    """``profile`` without any link to ``calendar_id``."""
    return {
        **profile,
        "calendar_links": [
            link
            for link in profile.get("calendar_links", ()) or ()
            if link.get("calendar_id") != calendar_id
        ],
    }


def _calendar_with_same_source(
    record: Mapping[str, Any],
    url: str | None,
    calendars: Mapping[str, Any],
    data: Mapping[str, Any],
) -> str | None:
    """The id of a registry calendar that already has this source, if any.

    Home Assistant calendars match on their entity; ICS feeds on their URL
    (compared in memory only, never logged).
    """
    urls = data.get(DATA_ICS_URLS, {})
    for calendar_id, calendar in calendars.items():
        if record.get("source_type") == "ha_calendar":
            if calendar.get("source_type") == "ha_calendar" and calendar.get(
                "calendar_entity_id"
            ) == record.get("calendar_entity_id"):
                return str(calendar_id)
        elif (
            url
            and calendar.get("source_type") == "ics"
            and urls.get(calendar_id) == url
        ):
            return str(calendar_id)
    return None


def _calendar_ids_of(profile: Mapping[str, Any]) -> set[str]:
    return {
        str(link["calendar_id"])
        for link in profile.get("calendar_links", ()) or ()
        if link.get("calendar_id")
    }


def _calendar_ids_in_use(profiles: Mapping[str, Any]) -> set[str]:
    return {
        calendar_id
        for profile in profiles.values()
        if isinstance(profile, Mapping)
        for calendar_id in _calendar_ids_of(profile)
    }


def _location_ids_of(profile: Mapping[str, Any]) -> set[str]:
    ids = (
        {str(profile["default_location_id"])}
        if profile.get("default_location_id")
        else set()
    )
    for link in profile.get("calendar_links", ()) or ():
        if link.get("location_id"):
            ids.add(str(link["location_id"]))
        ids.update(
            str(rule["location_id"])
            for rule in link.get("rules", ()) or ()
            if rule.get("location_id")
        )
    return ids


def _location_ids_in_use(profiles: Mapping[str, Any]) -> set[str]:
    return {
        location_id
        for profile in profiles.values()
        if isinstance(profile, Mapping)
        for location_id in _location_ids_of(profile)
    }


def _split_patterns(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated pattern list into a tuple, dropping blanks."""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _join_patterns(patterns: Iterable[str]) -> str:
    return ", ".join(patterns)


def _first_rule_field(draft: dict[str, Any], field: str) -> str:
    rules = draft.get("packing_rules", [])
    if rules:
        return str(rules[0].get(field, ""))
    return ""


def _fmt(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(TZ).strftime("%H:%M")
