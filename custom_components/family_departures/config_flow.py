"""Config flow and options flow for Family Departures (spec §2, §4.1-§4.3, §5.5).

The config flow creates the single household entry: a name and the home
coordinates. All ongoing configuration happens in the options flow, which is a
menu offering:

* **add/edit/remove profile** - profiles are fully user-defined. Adding a
  profile walks through name and source, destination and mode, margins and
  weekday mask, packing rules, quiet hours and channel scripts, and ends on a
  read-only test calculation that shows the plan for both car and transit (spec
  §4.3: the config flow must not hide SL settings for car-default profiles).
  A profile's id is a stable slug derived from its name at creation; renaming
  keeps the id. Removing a profile also drops its secret ICS URL and purges its
  device and entities.
* **settings** - the global dry-run flag and the optional test-recipient script
  (one ``script.*`` entity; during development every notification goes only
  there - spec Environment facts, T18).

Secret handling (review-privacy-security, spec §4.2): the ICS URL is entered as
a password-type field and stored only in ``entry.data`` under
``DATA_ICS_URLS``. It is never placed in ``entry.options``, in a form
description/title, or in an error message. Everything else lives in
``entry.options``.

Persistent profile settings change only here, never through entities (spec
§4.1); the entities built in T14 only write per-day overrides and the
notification switch. Saving the options triggers a reload so the coordinator
picks up the new configuration without stale timers.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
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
    DEFAULT_MIN_TRANSFER_MARGIN,
    DEFAULT_PARKING_WALK_MARGIN,
    DOMAIN,
    MODES,
    NOTIFICATION_CHANNELS,
    OPT_DRY_RUN,
    OPT_PROFILES,
    OPT_TEST_RECIPIENT_SCRIPT,
)
from .models import (
    ArrivalRequirement,
    DurationResult,
    Margins,
    Mode,
    ProfileConfig,
    SourceFilter,
)
from .planner import plan_fixed
from .providers.ics import IcsFetchError, IcsScheduleProvider, parse_ics
from .timeutil import TZ, combine_local, make_mission_id

_LOGGER = logging.getLogger(__name__)

SOURCE_TYPES = ("ics", "ha_calendar")
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

    VERSION = 1

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
                vol.Required("home_location"): selector.LocationSelector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return FamilyDeparturesOptionsFlow()


class FamilyDeparturesOptionsFlow(OptionsFlow):
    """Options flow: manage profiles and global notification settings."""

    def __init__(self) -> None:
        self._profile_id: str | None = None
        # Working copy of the profile under edit, built up across the steps.
        self._draft: dict[str, Any] = {}
        # Id pending removal, carried from remove_profile to remove_confirm.
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
                OPT_DRY_RUN, default=options.get(OPT_DRY_RUN, True)
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

    # -- Profile editing ----------------------------------------------------

    async def async_step_add_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Begin adding a brand-new profile; its id is derived from the name."""
        self._profile_id = None
        self._draft = {}
        return await self.async_step_profile_source()

    async def async_step_edit_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick an existing profile to edit; the id stays unchanged on save."""
        profiles = self._profiles()
        if not profiles:
            return self.async_abort(reason="no_profiles")
        if user_input is not None:
            self._profile_id = user_input["profile_id"]
            self._draft = dict(self._existing_profile(self._profile_id) or {})
            return await self.async_step_profile_source()

        schema = vol.Schema(
            {
                vol.Required("profile_id"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=_profile_options(profiles))
                )
            }
        )
        return self.async_show_form(step_id="edit_profile", data_schema=schema)

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
        """Confirm removal, then drop the profile, its ICS URL and its device."""
        assert self._remove_id is not None
        remove_id = self._remove_id
        if user_input is not None:
            # Drop the profile from the options.
            profiles = dict(self._profiles())
            profiles.pop(remove_id, None)
            options = dict(self.config_entry.options)
            options[OPT_PROFILES] = profiles

            # Drop its secret ICS URL from entry.data.
            new_data = dict(self.config_entry.data)
            ics_urls = dict(new_data.get(DATA_ICS_URLS, {}))
            ics_urls.pop(remove_id, None)
            new_data[DATA_ICS_URLS] = ics_urls
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )

            # Purge the profile's device; this cascades its entities.
            device_registry = dr.async_get(self.hass)
            device = device_registry.async_get_device(
                identifiers={(DOMAIN, f"{self.config_entry.entry_id}_{remove_id}")}
            )
            if device is not None:
                device_registry.async_remove_device(device.id)

            return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="remove_confirm",
            data_schema=vol.Schema({}),
        )

    async def async_step_profile_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the schedule source and (for ICS) validate it by a test fetch.

        The id is not known yet when adding a profile (it is derived from the
        name on save), so this step is driven entirely by ``self._draft``.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            source_type = user_input["source_type"]
            self._draft["name"] = user_input["name"]
            self._draft["source_type"] = source_type
            self._draft["exclude_patterns"] = _split_patterns(
                user_input.get("exclude_patterns", "")
            )
            self._draft["include_patterns"] = _split_patterns(
                user_input.get("include_patterns", "")
            )

            if source_type == "ha_calendar":
                entity_id = user_input.get("calendar_entity_id")
                if not entity_id:
                    errors["calendar_entity_id"] = "calendar_required"
                else:
                    self._draft["calendar_entity_id"] = entity_id
                    self._draft["ics_url"] = None
            else:  # ics
                stored = (
                    self._stored_ics_url(self._profile_id)
                    if self._profile_id is not None
                    else None
                )
                url = user_input.get("ics_url") or stored
                if not url:
                    errors["ics_url"] = "ics_required"
                else:
                    error = await self._validate_ics(url)
                    if error:
                        errors["ics_url"] = error
                    else:
                        self._draft["ics_url"] = url
                        self._draft["calendar_entity_id"] = None

            if not errors:
                return await self.async_step_profile_destination()

        default_name = self._draft.get("name", "")
        default_source = self._draft.get("source_type", "ics")
        schema = vol.Schema(
            {
                vol.Required("name", default=default_name): selector.TextSelector(),
                vol.Required(
                    "source_type", default=default_source
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(SOURCE_TYPES),
                        translation_key="source_type",
                    )
                ),
                # ICS URL is a secret: password field, never pre-filled with the
                # token. An empty value on edit keeps the stored URL.
                vol.Optional("ics_url"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(
                    "calendar_entity_id",
                    description={
                        "suggested_value": self._draft.get("calendar_entity_id")
                    },
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="calendar")
                ),
                vol.Optional(
                    "exclude_patterns",
                    default=_join_patterns(self._draft.get("exclude_patterns", ())),
                ): selector.TextSelector(),
                vol.Optional(
                    "include_patterns",
                    default=_join_patterns(self._draft.get("include_patterns", ())),
                ): selector.TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="profile_source", data_schema=schema, errors=errors
        )

    async def async_step_profile_destination(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Destination, default travel mode and all mode settings.

        SL settings (walk/transfer margins) stay available regardless of the
        chosen default mode so a car-default profile still has a
        working transit plan (spec §4.3).
        """
        if user_input is not None:
            location = user_input["destination_location"]
            self._draft["dest_lat"] = location["latitude"]
            self._draft["dest_lon"] = location["longitude"]
            self._draft["default_mode"] = user_input["default_mode"]
            self._draft["static_minutes"] = user_input.get("static_minutes")
            self._draft["static_label"] = user_input.get("static_label") or None
            self._draft["weather_adjust"] = user_input["weather_adjust"]
            self._draft["car_fallback_minutes"] = user_input.get("car_fallback_minutes")
            return await self.async_step_profile_margins()

        dest_default = None
        if "dest_lat" in self._draft and "dest_lon" in self._draft:
            dest_default = {
                "latitude": self._draft["dest_lat"],
                "longitude": self._draft["dest_lon"],
            }
        schema = vol.Schema(
            {
                vol.Required(
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
        return self.async_show_form(step_id="profile_destination", data_schema=schema)

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
        """Quiet hours, per-channel scripts and notification toggles."""
        if user_input is not None:
            self._draft["quiet_start"] = user_input["quiet_start"]
            self._draft["quiet_end"] = user_input["quiet_end"]
            self._draft["notifications_enabled"] = user_input["notifications_enabled"]
            self._draft["evening_notice_enabled"] = user_input["evening_notice_enabled"]
            self._draft["change_threshold_minutes"] = int(
                user_input["change_threshold_minutes"]
            )
            self._draft["person_entity_id"] = user_input.get("person_entity_id") or None
            scripts: dict[str, str] = {}
            for channel in NOTIFICATION_CHANNELS:
                value = user_input.get(f"script_{channel}")
                if value:
                    scripts[channel] = value
            self._draft["scripts"] = scripts
            return await self.async_step_profile_test()

        existing_scripts = self._draft.get("scripts", {})
        script_selector = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="script")
        )
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
                "person_entity_id",
                description={"suggested_value": self._draft.get("person_entity_id")},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="person")),
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
        shows a transit plan (spec §4.1 pt 7, §4.3).
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
        """Write the draft profile into options/data and finish the flow.

        On add (``self._profile_id`` is None) the id is a stable slug derived
        from the name; on edit the existing id is kept unchanged.
        """
        draft = self._draft
        if self._profile_id is None:
            self._profile_id = _generate_profile_id(
                draft["name"], self._profiles().keys()
            )
        profile_id = self._profile_id

        # Secret ICS URL goes to entry.data only; strip it from the options
        # payload (review-privacy-security, §4.2).
        new_data = dict(self.config_entry.data)
        ics_urls = dict(new_data.get(DATA_ICS_URLS, {}))
        if draft.get("source_type") == "ics" and draft.get("ics_url"):
            ics_urls[profile_id] = draft["ics_url"]
        else:
            ics_urls.pop(profile_id, None)
        new_data[DATA_ICS_URLS] = ics_urls

        profiles = dict(self._profiles())
        profiles[profile_id] = {
            "id": profile_id,
            "name": draft["name"],
            "source_type": draft["source_type"],
            "calendar_entity_id": draft.get("calendar_entity_id"),
            "exclude_patterns": list(draft.get("exclude_patterns", ())),
            "include_patterns": list(draft.get("include_patterns", ())),
            "destination_id": f"{profile_id}_destination",
            "is_adult": draft.get("is_adult", False),
            "dest_lat": draft["dest_lat"],
            "dest_lon": draft["dest_lon"],
            "default_mode": draft["default_mode"],
            "static_minutes": draft.get("static_minutes"),
            "static_label": draft.get("static_label"),
            "weather_adjust": draft["weather_adjust"],
            "car_fallback_minutes": draft.get("car_fallback_minutes"),
            "arrival": draft["arrival"],
            "departure": draft["departure"],
            "boarding": draft["boarding"],
            "parking_and_walk": draft["parking_and_walk"],
            "min_transfer": draft["min_transfer"],
            "weekday_mask": list(draft.get("weekday_mask", DEFAULT_WEEKDAY_MASK)),
            "packing_rules": draft.get("packing_rules", []),
            "person_entity_id": draft.get("person_entity_id"),
            "notifications_enabled": draft["notifications_enabled"],
            "evening_notice_enabled": draft["evening_notice_enabled"],
            "change_threshold_minutes": draft["change_threshold_minutes"],
            "quiet_start": draft["quiet_start"],
            "quiet_end": draft["quiet_end"],
            "scripts": draft.get("scripts", {}),
        }

        options = dict(self.config_entry.options)
        options[OPT_PROFILES] = profiles

        # Updating entry.data separately so the secret URL lands in data, not
        # options; the options flow's create_entry only writes options.
        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
        return self.async_create_entry(data=options)

    # -- Helpers ------------------------------------------------------------

    @callback
    def _profiles(self) -> dict[str, Any]:
        return dict(self.config_entry.options.get(OPT_PROFILES, {}))

    @callback
    def _existing_profile(self, profile_id: str) -> dict[str, Any] | None:
        profile = self._profiles().get(profile_id)
        if profile is None:
            return None
        # Flatten the stored shape back into the draft shape used by the steps.
        draft = dict(profile)
        draft["ics_url"] = None
        return draft

    @callback
    def _stored_ics_url(self, profile_id: str) -> str | None:
        urls = self.config_entry.data.get(DATA_ICS_URLS, {})
        url = urls.get(profile_id)
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

    async def _packing_preview(self) -> str:
        """Describe which upcoming days a packing rule would match (§4.1 pt 4)."""
        rules = self._draft.get("packing_rules", [])
        match = ""
        if rules:
            match = rules[0].get("match", "")
        if not match:
            return "Ingen regel angiven."

        source_type = self._draft.get("source_type")
        provider: Any = None
        if source_type == "ics" and self._draft.get("ics_url"):
            provider = IcsScheduleProvider(
                async_get_clientsession(self.hass),
                self._draft["ics_url"],
                SourceFilter(),
                "preview",
            )
        elif source_type == "ha_calendar" and self._draft.get("calendar_entity_id"):
            from .providers.ha_calendar import HaCalendarScheduleProvider

            provider = HaCalendarScheduleProvider(
                self.hass,
                self._draft["calendar_entity_id"],
                SourceFilter(),
                "preview",
            )
        if provider is None:
            return "Spara källan först för förhandsvisning."

        folded = match.casefold()
        today = datetime.now(UTC).astimezone(TZ).date()
        hits: list[str] = []
        for offset in range(PACKING_PREVIEW_DAYS):
            day = today + timedelta(days=offset)
            try:
                result = await provider.async_get_day(day)
            except Exception:  # noqa: BLE001 - preview must never break the flow
                return "Kunde inte förhandsvisa (kontrollera källan)."
            if any(folded in event.summary.casefold() for event in result.events):
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
        """
        preview_id = self._profile_id or (
            _generate_profile_id(self._draft.get("name", ""), self._profiles().keys())
        )
        try:
            profile = _profile_from_draft(preview_id, self._draft)
        except (KeyError, ValueError) as err:
            _LOGGER.debug("Test calculation skipped: %s", err)
            return "Testberäkning ej tillgänglig (ofullständig profil)."

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
            destination_id=profile.destination_id,
            source="preview",
        )
        now = datetime.now(UTC)
        lines = [
            f"Destination: {profile.destination_id}",
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
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _generate_profile_id(name: str, existing: Iterable[str]) -> str:
    """Derive a stable slug id from a profile name, unique within ``existing``.

    The id is generated once at creation and stays stable across renames. On a
    collision a numeric suffix (``_2``, ``_3``, ...) is appended.
    """
    taken = set(existing)
    base = slugify(name) or "profile"
    if base not in taken:
        return base
    suffix = 2
    while f"{base}_{suffix}" in taken:
        suffix += 1
    return f"{base}_{suffix}"


def _profile_options(
    profiles: dict[str, Any],
) -> list[selector.SelectOptionDict]:
    """Build SelectSelector options over configured profiles (value=id)."""
    return [
        selector.SelectOptionDict(
            value=pid,
            label=str(profile.get("name", pid)),
        )
        for pid, profile in profiles.items()
    ]


def _minutes_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=120, mode=selector.NumberSelectorMode.BOX
        )
    )


def _split_patterns(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated pattern list into a tuple, dropping blanks."""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _join_patterns(patterns: tuple[str, ...] | list[str]) -> str:
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


def _parse_time(value: Any) -> time:
    """Parse a HH:MM[:SS] string (TimeSelector output) into a ``time``."""
    if isinstance(value, time):
        return value
    return time.fromisoformat(str(value))


def _profile_from_draft(profile_id: str, draft: dict[str, Any]) -> ProfileConfig:
    """Build a ProfileConfig from the working draft (used for the test calc)."""
    return ProfileConfig(
        id=profile_id,
        name=draft["name"],
        source_type=draft["source_type"],
        calendar_entity_id=draft.get("calendar_entity_id"),
        source_filter=SourceFilter(
            exclude_patterns=tuple(draft.get("exclude_patterns", ())),
            include_patterns=tuple(draft.get("include_patterns", ())),
        ),
        destination_id=draft.get("destination_id", f"{profile_id}_destination"),
        dest_lat=float(draft["dest_lat"]),
        dest_lon=float(draft["dest_lon"]),
        default_mode=draft["default_mode"],
        static_minutes=draft.get("static_minutes"),
        static_label=draft.get("static_label"),
        weather_adjust=draft.get("weather_adjust", False),
        car_fallback_minutes=draft.get("car_fallback_minutes"),
        margins=Margins(
            arrival=int(draft["arrival"]),
            departure=int(draft["departure"]),
            boarding=int(draft["boarding"]),
            parking_and_walk=int(draft["parking_and_walk"]),
            min_transfer=int(draft["min_transfer"]),
        ),
        weekday_mask=frozenset(int(d) for d in draft.get("weekday_mask", ())),
        packing_rules=(),
        person_entity_id=draft.get("person_entity_id"),
        notifications_enabled=draft.get("notifications_enabled", False),
        change_threshold_minutes=int(
            draft.get("change_threshold_minutes", DEFAULT_CHANGE_THRESHOLD_MINUTES)
        ),
        quiet_start=_parse_time(draft.get("quiet_start", "22:00:00")),
        quiet_end=_parse_time(draft.get("quiet_end", "06:00:00")),
        scripts=dict(draft.get("scripts", {})),
        evening_notice_enabled=draft.get("evening_notice_enabled", True),
    )
