"""The example dashboard loads and only uses §9 entities (T20, spec §13).

The dashboard in ``examples/dashboard.yaml`` is built from Home Assistant's
standard cards. These tests check two acceptance points for T20:

* the YAML parses into a Lovelace dashboard structure (a mapping with a
  ``views`` list of card-bearing views), and
* every entity id it references maps to one of the §9 entities for one of the
  four stable profiles -- so the example never points at an entity the
  integration does not create.

The set of valid entity ids is derived from the integration's own
``strings.json`` entity names (which drive the default entity ids) rather than
hard-coded here, so the test tracks any future rename.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from homeassistant.util import slugify

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = REPO_ROOT / "examples" / "dashboard.yaml"
STRINGS = REPO_ROOT / "custom_components" / "family_departures" / "strings.json"

PROFILE_IDS = ("kid_a", "kid_b", "parent_a", "parent_b")

# An entity id in a card config, e.g. ``sensor.kid_a_recommended_leave_time``.
_ENTITY_ID_RE = re.compile(r"\b([a-z_]+)\.([a-z0-9_]+)\b")


def _valid_entity_ids() -> set[str]:
    """Default entity ids the integration creates, from the §9 entity names.

    Entity ids are generated as ``<domain>.<profile>_<slug(name)>`` because
    every entity uses ``has_entity_name`` under a per-profile device.
    """
    data = json.loads(STRINGS.read_text(encoding="utf-8"))
    valid: set[str] = set()
    for domain, keys in data["entity"].items():
        for meta in keys.values():
            name_slug = slugify(meta["name"])
            for profile in PROFILE_IDS:
                valid.add(f"{domain}.{profile}_{name_slug}")
    return valid


def _referenced_entity_ids(node: object) -> set[str]:
    """Collect every ``entity``/``entities`` id reachable in the config tree."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("entity", "entities") and isinstance(value, str):
                found.add(value)
            else:
                found |= _referenced_entity_ids(value)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, str):
                # ``entities: [sensor.x, ...]`` short form.
                if _ENTITY_ID_RE.fullmatch(item):
                    found.add(item)
            else:
                found |= _referenced_entity_ids(item)
    return found


def test_dashboard_yaml_loads_as_lovelace_config() -> None:
    """The example parses into a dashboard with views that carry cards."""
    config = yaml.safe_load(DASHBOARD.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    views = config.get("views")
    assert isinstance(views, list) and views
    for view in views:
        assert isinstance(view, dict)
        assert "cards" in view and isinstance(view["cards"], list)
        for card in view["cards"]:
            assert isinstance(card, dict)
            assert "type" in card


def test_dashboard_only_references_section9_entities() -> None:
    """Every entity id in the dashboard is a §9 entity for a known profile."""
    config = yaml.safe_load(DASHBOARD.read_text(encoding="utf-8"))
    referenced = _referenced_entity_ids(config)
    # Drop helper entities an installer must create (the example mentions none
    # in its entity rows; input_booleans live only in scripts.yaml).
    assert referenced, "no entities referenced in the dashboard"
    valid = _valid_entity_ids()
    unknown = sorted(e for e in referenced if e not in valid)
    assert not unknown, f"dashboard references non-§9 entities: {unknown}"


def test_dashboard_has_no_real_data() -> None:
    """No coordinates, ICS tokens or feed URLs leak into the example."""
    text = DASHBOARD.read_text(encoding="utf-8")
    assert "ical-feed/parent/" not in text
    assert "schoolsoft.se" not in text
    assert "vklass" not in text.lower()
    # No high-precision Nordic coordinate (same shape test_no_secrets uses).
    assert re.search(r"\b5[5-9]\.\d{4,}\b", text) is None
