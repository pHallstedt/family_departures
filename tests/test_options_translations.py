"""The options flow's strings exist for every step and agree across languages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from custom_components.family_departures.config_flow import (
    FamilyDeparturesOptionsFlow,
)

COMPONENT = Path(__file__).parent.parent / "custom_components" / "family_departures"


def _load(name: str) -> dict[str, Any]:
    return json.loads((COMPONENT / name).read_text(encoding="utf-8"))["options"]


def _keys(node: Any, prefix: str = "") -> set[str]:
    if not isinstance(node, dict):
        return {prefix}
    found: set[str] = set()
    for key, value in node.items():
        found |= _keys(value, f"{prefix}/{key}")
    return found


# Menu choices that hand straight on to a form of their own and show nothing.
_PASS_THROUGH = {"add_profile", "location_add", "calendar_add"}


def _flow_steps() -> set[str]:
    return {
        name.removeprefix("async_step_")
        for name in dir(FamilyDeparturesOptionsFlow)
        if name.startswith("async_step_")
    } - _PASS_THROUGH


def test_every_options_step_has_a_title_in_every_language() -> None:
    for name in ("strings.json", "translations/en.json", "translations/sv.json"):
        steps = _load(name)["step"]
        missing = {
            step
            for step in _flow_steps()
            if step not in steps or "title" not in steps[step]
        }
        assert not missing, f"{name} lacks strings for {sorted(missing)}"


def test_options_strings_match_between_languages() -> None:
    base = _keys(_load("strings.json"))
    assert _keys(_load("translations/en.json")) == base
    assert _keys(_load("translations/sv.json")) == base
