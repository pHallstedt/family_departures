"""Public reference coordinates for SL journey tests.

Single source of truth for the two coordinate pairs used by the SL Journey
Planner tests and fixtures. These are **public** central-Stockholm reference
points (well-known stations), established as public in the T00 investigation
(``docs/decisions/sl-realtime.md``). They are deliberately *not* a real home or
school address.

The secret scanner (``tests/test_no_secrets.py``) imports ``PUBLIC_COORD_STRINGS``
from this module so these exact, known-public values are permitted while any
other Nordic-looking coordinate still trips the guard. Keep this list tight: add
a value here only when it is a verified public reference point.
"""

from __future__ import annotations

# Longitude-first coordinate pairs as (lat, lon). Central Stockholm stations.
ORIGIN: tuple[float, float] = (59.33258, 18.06490)
DESTINATION: tuple[float, float] = (59.34300, 18.04960)

# The exact decimal strings that may legitimately appear in tracked files for
# these public points. The scanner allows these literals (and nothing broader).
PUBLIC_COORD_STRINGS: frozenset[str] = frozenset(
    {
        "59.33258",
        "59.34300",
    }
)
